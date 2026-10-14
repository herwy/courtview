import sys
from importlib.metadata import distribution, PackageNotFoundError
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
todo = [Requirement(l) for l in open(sys.argv[1]).read().split("\n") if l.strip() and not l.startswith("#")]
seen = {}
while todo:
    req = todo.pop()
    if req.marker and not req.marker.evaluate({"extra": ""}): continue
    name = canonicalize_name(req.name)
    if name in seen: continue
    try: dist = distribution(name)
    except PackageNotFoundError: sys.exit(f"MISSING {name}")
    seen[name] = dist.version
    todo.extend(Requirement(r) for r in dist.requires or [])
if not seen: sys.exit("no packages resolved")
for n, v in sorted(seen.items()): print(f"{n}=={v}")
