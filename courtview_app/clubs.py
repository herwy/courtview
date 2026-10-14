"""Club configuration: the single source of truth for every club CourtView knows about.

Python sets (heatmap, payments, archive) and the dashboard's club list (served at /api/clubs)
are derived from CLUBS. Bottom layer: stdlib only, no imports from the rest of the package.

Racketeer migrated off Padelmates to a different booking app (2026-07-01), so it is archived:
no background polling, 410 guard on live routes, snapshots served from /api/archive/*.
"""

CLUBS = (
    {"id": "5111764d9bb14be3adbdb8e133e8bd80", "name": "Racketeer", "sub": "North Acton, London",
     "short": "Racketeer", "courts": 11, "status": "archived",
     "payments": False, "heatmap": False, "compare": False},
    {"id": "47d2eb0db7194a9dbd29783c3a2a82ad", "name": "Padium Canary Wharf", "sub": "Canary Wharf, London",
     "short": "Padium · Canary Wharf", "courts": 7, "status": "live",
     "payments": True, "heatmap": True, "compare": True},
    {"id": "788fa2c66535421aabc60fd27f941c42", "name": "Rocket Padel Ilford", "sub": "Ilford, London",
     "short": "Rocket Padel · Ilford", "courts": 12, "status": "live",
     "payments": True, "heatmap": True, "compare": True},
)

PUBLIC_KEYS = ("id", "name", "sub", "short", "courts", "archived", "compare")

# Clubs and court counts for server-side heatmap fetching (archived clubs excluded)
HEATMAP_CLUBS = [{"id": c["id"], "courts": c["courts"]} for c in CLUBS if c["heatmap"]]

# Clubs whose payments are synced into the local `payments` table (never the archived club)
PAYMENT_SYNC_CLUB_IDS = frozenset(c["id"] for c in CLUBS if c["payments"])
PAYMENT_CLUB_IDS = PAYMENT_SYNC_CLUB_IDS

ARCHIVE_CLUB_IDS = frozenset(c["id"] for c in CLUBS if c["status"] == "archived")
if len(ARCHIVE_CLUB_IDS) != 1:
    raise RuntimeError("clubs.CLUBS must contain exactly one archived club")
RACKETEER_CLUB_ID = next(iter(ARCHIVE_CLUB_IDS))


def public_clubs():
    """Club list for the dashboard: whitelisted keys only, fresh dicts."""
    return [
        {"id": c["id"], "name": c["name"], "sub": c["sub"], "short": c["short"],
         "courts": c["courts"], "archived": c["status"] == "archived", "compare": bool(c["compare"])}
        for c in CLUBS
    ]
