---
phase: quick
plan: 260522-wfp
type: execute
wave: 1
depends_on: []
files_modified:
  - courtview.py
  - courtview.html
autonomous: true
requirements: [add-stratford-tpc]
must_haves:
  truths:
    - "Stratford Padel Club appears in the club selector alongside the three Padelmates clubs"
    - "Availability tab shows per-court, per-slot grids for Stratford using the same v1 court format"
    - "Stats tab shows the occupancy heatmap for Stratford (built from 14 days of ObtenerHorariosDisponibles)"
    - "Club Info tab shows Stratford's name, contact, and court list (no membership plans)"
    - "Compare tab includes Stratford in the club list"
    - "Revenue and Stats mix/coach sections show empty-state messages for Stratford (not blank crashes)"
    - "All existing Padelmates club flows are completely unchanged"
  artifacts:
    - path: "courtview.py"
      provides: "TPC auth, ObtenerPistasDisponibles3, ObtenerHorariosDisponibles handlers; /api/month and /api/heatmap TPC routing; /api/club-info TPC stub; background heatmap thread for Stratford"
    - path: "courtview.html"
      provides: "Stratford entry in CLUBS and COMPARE_CLUBS arrays with platform:'tpc' marker; activity/revenue empty-state guards for TPC clubs"
  key_links:
    - from: "courtview.html CLUBS array"
      to: "/api/month?club_id=stratfordpadelclub"
      via: "same fetchDay/api_month path, TPC routing inside api_month"
    - from: "/api/month TPC branch"
      to: "availability SQLite table"
      via: "store_cached using club_id='stratfordpadelclub'"
    - from: "/api/heatmap TPC branch"
      to: "heatmap_cache SQLite table"
      via: "_fetch_tpc_heatmap writes same schema as Padelmates heatmap"
---

<objective>
Add Stratford Padel Club (TPC Matchpoint platform) to CourtView as a fourth selectable club.

Purpose: The club uses TPC Matchpoint (tpc-informatica.es), not Padelmates, so it needs its own
API layer. All four dashboard tabs must work; tabs that rely on Padelmates-only data (activity mix,
coach stats, revenue) degrade gracefully with empty-state messages for TPC clubs.

Output: courtview.py gains TPC fetch helpers and routing branches. courtview.html gains Stratford
in the club selector and guarded rendering for Padelmates-only sections.
</objective>

<execution_context>
@$HOME/.claude/get-shit-done/workflows/execute-plan.md
@$HOME/.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@/Users/herwy/dev/courtview/CLAUDE.md
@/Users/herwy/dev/courtview/courtview.py
@/Users/herwy/dev/courtview/courtview.html
</context>

<interfaces>
<!-- Key contracts the executor needs. Extracted from codebase. -->

TPC Matchpoint auth endpoint (no rotation - token is always "autorizado"):
  POST https://movhub.matchpoint.com.es/services/mobi/configservices/v1/auth.svc/Autorizar
  Body: {"passcode":"passcode"}
  Response body (raw string): "\"autorizado\""
  Use the literal string "autorizado" as the token header value on all club API calls.

TPC availability endpoint (per-court, per-slot):
  POST https://stratfordpadelclub.matchpoint.com.es/services/mobi/appservices/v1/reservas.svc/ObtenerPistasDisponibles3
  Headers: {"Accept":"application/json","Content-type":"application/json","token":"autorizado"}
  Body: {"idCentro":2,"idTipoPista":0,"idDeporte":0,"idCuadro":4,
         "strfecha":"DD/MM/YYYY","strhora":"00:00","duracionPartido":90,
         "mostrarDiaEntero":true,"numeroResultadosPorPista":20,
         "coordenadasusuario":"","distmax":0,"poblacion":""}
  Date format: dd/MM/yyyy (Spanish format, NOT ISO)
  Response: {"Correcto":true,"Listado":[{
    "Id_Pista":16,"Nombre_Recurso":"Court 9: Roofed Outdoor",
    "StrFecha":"23/05/2026","StrHora_Inicio":"09:30","StrHora_Fin":"11:00",
    "Precio":88.0,"PrecioCliente":22.0,"Cubierta":true,
    "Tipo_Pista":"Indoor","Sub_Tipo_Pista":"Blue Lawn"},...]}

TPC hourly aggregate endpoint (for heatmap - shows booked/available per hour):
  POST https://stratfordpadelclub.matchpoint.com.es/services/mobi/appservices/v1/reservas.svc/ObtenerHorariosDisponibles
  Headers: same as above
  Body: {"idCentro":2,"idDeporte":0,"idTipoPista":0,"idCuadro":4,
         "strfecha":"DD/MM/YYYY","duracionPartido":90}
  Response: {"Correcto":true,"Listado":[{"Hora":"9:00","Disponible":false,"Fecha":"23/05/2026"},...]}
  CRITICAL: Disponible:false means BOOKED (slot not available). Use NOT Disponible for occupancy.

Constants to add near the top of courtview.py (after HEATMAP_CLUBS):
  TPC_BASE_URL    = "https://stratfordpadelclub.matchpoint.com.es"
  TPC_AUTH_URL    = "https://movhub.matchpoint.com.es/services/mobi/configservices/v1/auth.svc/Autorizar"
  TPC_TOKEN       = "autorizado"   # static, never rotates
  TPC_CENTRO_ID   = 2
  TPC_CUADRO_ID   = 4
  TPC_CLUB_ID     = "stratfordpadelclub"

SQLite availability table schema (existing - TPC stores here too):
  (club_id TEXT, start_datetime TEXT, end_datetime TEXT, payload TEXT, fetched_at INTEGER)
  PRIMARY KEY (club_id, start_datetime, end_datetime)

v1 court format the frontend expects (returned by api_month / fetchDay):
  [{"court_id":"16","court_name":"Court 9: Roofed Outdoor","name":"Court 9: Roofed Outdoor",
    "sport_type":"PADEL","available_slots":[{
      "start_datetime":1748000400000,  <- epoch ms in BST (Europe/London)
      "end_datetime":1748005800000,    <- epoch ms in BST
      "interval_prices":[{"duration":90,"price":88.0}],
      "price_90":88.0}]}]

Timestamp conversion (TPC "HH:MM" on date "YYYY-MM-DD" to epoch ms):
  from datetime import datetime
  from zoneinfo import ZoneInfo
  _LONDON_TZ = ZoneInfo("Europe/London")  # already in scope
  dt = datetime(year, month, day, hour, minute, tzinfo=_LONDON_TZ)
  ms = int(dt.timestamp() * 1000)

Heatmap DB schema (existing - TPC writes same tables):
  heatmap_cache        (club_id, dow, hour, avg_occ, samples, fetched_at) PK (club_id,dow,hour)
  heatmap_hour_signal  (club_id, hour, norm, fetched_at)                  PK (club_id,hour)
  heatmap_dow_signal   (club_id, dow, norm, fetched_at)                   PK (club_id,dow)

Existing helpers available in scope:
  store_cached(club_id, start_ms_str, end_ms_str, payload_json_str) -> None
  _db_connect() -> sqlite3.Connection
  _gate() -> (passed: bool, via_query: str)
  _forbidden() -> Response
  _set_cookie(resp, token_str) -> None
  _LONDON_TZ, HEATMAP_HOURS (range 7-22), HEATMAP_STALE_SECS
  HEATMAP_CLUBS list (add Stratford entry here for heatmap refresh loop)
</interfaces>

<tasks>

<task type="auto">
  <name>Task 1: TPC backend - fetch helpers, api_month routing, heatmap, club-info stub</name>
  <files>courtview.py</files>
  <action>
Add TPC Matchpoint support to courtview.py. All changes are additive; existing Padelmates
code paths must not be touched.

**1. Constants block** (after HEATMAP_CLUBS, before HEATMAP_HOURS):

Add: TPC_BASE_URL, TPC_AUTH_URL, TPC_TOKEN, TPC_CENTRO_ID, TPC_CUADRO_ID, TPC_CLUB_ID as
described in the interfaces block. Add Stratford to HEATMAP_CLUBS:
  {"id": "stratfordpadelclub", "courts": 9, "platform": "tpc"}

**2. Helper: _tpc_post(endpoint_path, body_dict) -> dict**

A thin urllib.request wrapper. Build the full URL as TPC_BASE_URL + endpoint_path. Encode
body_dict as UTF-8 JSON. Set headers Accept, Content-type, token (all as per interface).
Call urllib.request.urlopen with timeout=15. Read and JSON-decode the response. Return the
parsed dict. Raise on HTTP errors or non-Correcto responses. Use only stdlib (urllib.request,
json) - no cffi_requests.

**3. Helper: _tpc_date(date_obj) -> str**

Convert a datetime.date to "DD/MM/YYYY" (Spanish TPC format). Trivial: return
date_obj.strftime("%d/%m/%Y").

**4. Helper: _fetch_tpc_day(date_obj) -> list**

Calls _tpc_post for ObtenerPistasDisponibles3 with the correct body (see interfaces).
Groups the Listado by Id_Pista. For each unique court builds a v1 court dict:
  court_id = str(item["Id_Pista"])
  court_name = item["Nombre_Recurso"]
  name = court_name
  sport_type = "PADEL"

For each slot in that court's entries, parse StrHora_Inicio and StrHora_Fin as HH:MM strings
on date_obj. Convert to epoch ms using _LONDON_TZ (see interfaces for the exact pattern).
Build available_slots entry:
  {"start_datetime": start_ms, "end_datetime": end_ms,
   "interval_prices": [{"duration": 90, "price": item["Precio"]}],
   "price_90": item["Precio"]}

Return the list of court dicts. Return [] on any exception (log with print).

**5. Route: api_month TPC branch**

In the existing api_month() handler, immediately after club_id is validated and before the
cutoff/today lines, add an early-exit branch:

  if club_id == TPC_CLUB_ID:
      return _api_month_tpc(club_id, via_query)

Implement _api_month_tpc(club_id, via_query) as a standalone function (not inside the route).
It replicates the same date_params loop (28 days) and SQLite cache read logic as api_month,
but on cache miss calls _fetch_tpc_day(date_obj) and stores with store_cached.

The payload stored per day is json.dumps(courts_list) - the raw v1 list, not a v2/v3 wrapper.
This is what the frontend expects directly (normaliseV2 passes through non-v2 data unchanged).

**6. Helper: _fetch_tpc_heatmap() -> None**

Fetch 14 days of ObtenerHorariosDisponibles starting from today. For each day call _tpc_post
for that endpoint (see interface for body). Accumulate occupancy counts per day-of-week (0=Mon,
6=Sun, use date.weekday()) and per hour.

From Listado: for each item, if not item["Disponible"] that slot is booked. Parse the hour
from item["Hora"] (format "H:MM" - split on ":" take index 0, cast to int). Clamp to
HEATMAP_HOURS. Increment booked[dow][hour] and total[dow][hour] counters.

After all 14 days, compute occupancy rates (booked/total per cell, default 0 if total==0).
Normalise hour signal and DOW signal (divide by max). Write to heatmap_cache,
heatmap_hour_signal, heatmap_dow_signal, and court_popularity tables using the same INSERT OR
REPLACE pattern as _fetch_heatmap_for_club. For court_popularity, accumulate booked counts
per Nombre_Recurso from the hourly data (or derive from _fetch_tpc_day calls for today if
needed - hourly data doesn't carry court names, so either skip court_popularity or derive from
ObtenerPistasDisponibles3 for today only). Use club_id = TPC_CLUB_ID. Log progress with print.

**7. _heatmap_refresh_loop: TPC dispatch**

In _heatmap_refresh_loop, change the refresh call to dispatch on platform:
  for club in HEATMAP_CLUBS:
      if club.get("platform") == "tpc":
          _fetch_tpc_heatmap()
      else:
          _fetch_heatmap_for_club(club["id"], club["courts"])

Apply the same dispatch in the startup stale-check block at the top of the loop.
Also update _heatmap_is_stale to accept TPC_CLUB_ID (it queries heatmap_cache by club_id so
it already works if TPC_CLUB_ID is passed directly).

**8. api_club_info TPC branch**

In api_club_info(), after club_id is validated, add:
  if club_id == TPC_CLUB_ID:
      return _api_club_info_tpc(club_id, via_query)

Implement _api_club_info_tpc returning a cached-or-live response. The TPC club info is static
(no Padelmates API calls needed). Return:
  {"profile": {"name": "Stratford Padel Club",
               "address": "Stratford, London",
               "phone": "00447365809000",
               "email": "info@stratfordpadelclub.org",
               "sport_types": ["PADEL"]},
   "memberships": [],
   "credits": [],
   "extras": []}

Cache this in club_info_cache (same table, same 10-minute TTL) so the cache read at the top
of api_club_info serves it on repeat requests.

**9. api_activity_summary and api_revenue_summary TPC stubs**

Add early-exit branches in api_activity_summary and api_revenue_summary:
  if club_id == TPC_CLUB_ID:
      resp = Response(json.dumps({"total": 0, "counts": {}, "coach_rates": {}, "range_days": 0}),
                      status=200, content_type="application/json")
      if via_query: _set_cookie(resp, via_query)
      return resp

For api_revenue_summary return:
  {"total_combined_payments": None, "platform_not_supported": True}

No TPC club_id check needed in api_payment_history - returning 400 for missing/unknown
club_id is already its behavior; optionally add a stub there too returning [].
  </action>
  <verify>
    <automated>cd /Users/herwy/dev/courtview && python3 -c "import ast, sys; ast.parse(open('courtview.py').read()); print('syntax OK')"</automated>
  </verify>
  <done>
    - courtview.py parses without syntax errors
    - TPC_CLUB_ID, TPC_BASE_URL, TPC_TOKEN constants present
    - _fetch_tpc_day, _tpc_post, _fetch_tpc_heatmap functions defined
    - api_month has TPC branch routing to _api_month_tpc
    - api_club_info has TPC branch
    - api_activity_summary and api_revenue_summary have TPC early-exit stubs
    - HEATMAP_CLUBS includes stratfordpadelclub with platform:"tpc"
  </done>
</task>

<task type="auto">
  <name>Task 2: Frontend - add Stratford to CLUBS/COMPARE_CLUBS, guard TPC-incompatible sections</name>
  <files>courtview.html</files>
  <action>
Three targeted edits to courtview.html. No existing logic is removed.

**Edit 1: CLUBS array (line ~847)**

Add Stratford as a fourth entry:
  { id: 'stratfordpadelclub', name: 'Stratford Padel Club', sub: 'Stratford, London',
    courts: 9, platform: 'tpc' }

The id must exactly match TPC_CLUB_ID in courtview.py.

**Edit 2: COMPARE_CLUBS array (line ~871)**

Add:
  { id: 'stratfordpadelclub', name: 'Stratford Padel · Stratford' }

**Edit 3: TPC club detection helper and Stats/Revenue guards**

Immediately after the COMPARE_CLUBS declaration, add:
  function isTPCClub(id) { return id === 'stratfordpadelclub'; }

In loadStats() (the async function that fetches activity-summary and revenue/coach data for
the Stats tab): before the parallel fetch calls, add a guard. If isTPCClub(CLUB), skip the
activity-summary and coach-stats fetches, set mixPromise and coachPromise to
Promise.resolve(null), and render a message in statsMixOut and statsCoachOut:
  document.getElementById('statsMixOut').innerHTML =
    '<div class="empty">Activity mix not available for this club.</div>';
  document.getElementById('statsCoachOut').innerHTML =
    '<div class="empty">Coach stats not available for this club.</div>';

The heatmap fetch (loadRealHeatmap) still runs for TPC clubs - it reads from /api/heatmap
which the backend now populates via _fetch_tpc_heatmap.

In the Compare tab revenue fetch block (around line 2041 where it calls
/api/revenue-summary): the backend already returns {"total_combined_payments": null} for TPC
clubs. The frontend already handles null via `d?.total_combined_payments ?? null`. No change
needed there.

In the Compare tab activity fetch block (around line 2047): same - backend returns
{total: 0, ...} and frontend uses `d?.total_activities ?? null`. No change needed.

No other edits. Do not modify any rendering functions, CSS, or non-TPC code paths.
  </action>
  <verify>
    <automated>grep -c "stratfordpadelclub" /Users/herwy/dev/courtview/courtview.html</automated>
  </verify>
  <done>
    - courtview.html has at least 2 occurrences of "stratfordpadelclub" (CLUBS and COMPARE_CLUBS)
    - isTPCClub helper present
    - Stats tab guards present (statsMixOut and statsCoachOut get empty-state messages when isTPCClub)
    - No existing Padelmates club entries changed
  </done>
</task>

<task type="checkpoint:human-verify" gate="blocking">
  <what-built>
    Deploy courtview.py and courtview.html to the RPi via cv-deploy, restart gunicorn,
    then verify end-to-end:
    1. Run: cv-deploy
    2. Verify deploy landed: rpi 'grep -c "stratfordpadelclub" /root/projects/courtview/courtview.html'
    3. Verify py landed: rpi 'grep -c "TPC_CLUB_ID" /root/projects/courtview/courtview.py'
    4. Verify process running: rpi 'pgrep -fa "gunicorn.*courtview"'
    5. Smoke-test TPC availability endpoint from RPi (no cookie needed for this check, just
       confirm the route returns valid JSON):
       rpi 'TOKEN=$(cat /root/.courtview_token) && python3 -c "
import http.client, json
conn = http.client.HTTPConnection(\"192.168.8.175\", 8766, timeout=10)
conn.request(\"GET\", \"/api/month?club_id=stratfordpadelclub\", headers={\"Cookie\": \"courtview_token=$(cat /root/.courtview_token)\"})
r = conn.getresponse()
body = r.read()
d = json.loads(body)
print(\"status\", r.status, \"days keys\", len(d.get(\"days\", {})))
"'
    6. Open http://lab.doxx:8766/ in a browser, select Stratford Padel Club from the nav,
       navigate to the Availability tab and confirm courts appear with slots.
    7. Check Stats tab shows heatmap (may say "building" if background thread hasn't run yet)
       and "not available" messages for activity mix and coach stats.
    8. Check Club Info tab shows Stratford name and contact details.
    9. Check Compare tab includes Stratford in the club list.
    10. Switch back to Racketeer - confirm it still works normally.
  </what-built>
  <how-to-verify>
    Steps 1-5 are automated (run them). Steps 6-10 require a browser visit to
    http://lab.doxx:8766/ with the courtview token.

    Expected for step 5: status 200, days keys 28 (some may have null values if TPC is slow,
    but the response structure must be correct).

    Expected for browser check: Stratford appears as 4th club, courts visible on
    Availability tab, heatmap either shows data or "building" message (not an error crash),
    activity mix shows "not available", other clubs unaffected.
  </how-to-verify>
  <resume-signal>Type "approved" if all checks pass, or describe any issues found.</resume-signal>
</task>

</tasks>

<threat_model>
## Trust Boundaries

| Boundary | Description |
|----------|-------------|
| courtview.py -> TPC Matchpoint API | Outbound calls from RPi to Spanish third-party server |
| Browser -> courtview.py /api/month | Existing authenticated boundary; TPC club_id added to accepted set |

## STRIDE Threat Register

| Threat ID | Category | Component | Disposition | Mitigation Plan |
|-----------|----------|-----------|-------------|-----------------|
| T-tpc-01 | Tampering | TPC_CLUB_ID string as routing key | mitigate | Exact string comparison only; no wildcard. club_id == TPC_CLUB_ID not startswith or regex |
| T-tpc-02 | Information Disclosure | TPC static token "autorizado" in source | accept | Token is a public API passcode per APK reverse engineering; it unlocks no user PII, only public court availability |
| T-tpc-03 | Spoofing | Attacker passes club_id=stratfordpadelclub to existing Padelmates endpoints | accept | ALLOWED_PATHS whitelist still governs the generic proxy route; TPC branches are inside dedicated handlers only |
| T-tpc-04 | Denial of Service | _fetch_tpc_day called 28 times on cold cache | mitigate | timeout=15 per call; failures return [] and are caught; store_cached prevents repeat calls for cached days |
| T-tpc-SC | Tampering | No new npm/pip/cargo installs in this task | accept | No new dependencies; stdlib urllib.request only |
</threat_model>

<verification>
- Python syntax: python3 -c "import ast; ast.parse(open('courtview.py').read())"
- TPC constants present: grep -c "TPC_CLUB_ID" courtview.py (expect >= 1)
- Stratford in HTML: grep -c "stratfordpadelclub" courtview.html (expect >= 2)
- No Padelmates logic touched: git diff courtview.py | grep "^-" | grep -v "^---" | grep -v "HEATMAP_CLUBS\|heatmap_refresh_loop" - should show no removed lines from existing Padelmates functions
- Deploy verification on RPi per Task 3 steps
</verification>

<success_criteria>
- Stratford Padel Club selectable in dashboard nav as a 4th club
- Availability tab renders TPC court/slot grid in the same visual format as Padelmates clubs
- Stats tab heatmap populates from TPC occupancy data (or shows "building" on first load)
- Stats activity mix and coach stats show explicit "not available" messages for TPC clubs
- Club Info tab shows Stratford contact details
- Compare tab includes Stratford
- All three existing Padelmates clubs continue to function identically
- Deploy verified on RPi with grep confirmation and process check
</success_criteria>

<output>
Create `.planning/quick/260522-wfp-add-stratford-padel-club-tpc-matchpoint-/260522-wfp-SUMMARY.md` when done.
</output>
