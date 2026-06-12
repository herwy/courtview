---
phase: quick
plan: 260612-vxi
type: execute
wave: 1
depends_on: []
files_modified:
  - courtview.py
autonomous: true
requirements: [archive-racketeer-data]
must_haves:
  truths:
    - "Racketeer availability rows are never deleted by the 28-day TTL eviction in _refresh_loop"
    - "Every heatmap refresh appends a new dated snapshot row to archive_heatmap instead of overwriting"
    - "Every club_info fetch appends a new dated snapshot row to archive_club_info instead of overwriting"
    - "Every financial/revenue API call appends a new dated snapshot row to archive_financial instead of overwriting"
  artifacts:
    - path: "courtview.py"
      provides: "Archive tables created at startup, TTL eviction guard, archive INSERTs alongside existing live tables"
      contains: "archive_heatmap"
  key_links:
    - from: "_refresh_loop"
      to: "availability DELETE"
      via: "WHERE clause excludes Racketeer club_id"
      pattern: "RACKETEER_CLUB_ID"
    - from: "_fetch_heatmap_for_club"
      to: "archive_heatmap INSERT"
      via: "plain INSERT (not OR REPLACE) after existing INSERT OR REPLACE"
      pattern: "archive_heatmap"
---

<objective>
Permanently archive Racketeer availability, heatmap, club info, and financial data in
courtview.py so historical records accumulate rather than being overwritten or evicted.

Purpose: Racketeer data is the primary research target. Its availability rows must survive the
28-day TTL eviction, and all periodically-refreshed stats must build a timestamped history
rather than being silently overwritten by INSERT OR REPLACE.

Output: Modified courtview.py with three changes - (1) eviction guard, (2) three new archive
tables created at startup, (3) archive INSERT calls alongside all existing INSERT OR REPLACE
writes for the four data types in scope.
</objective>

<execution_context>
@$HOME/.claude/get-shit-done/workflows/execute-plan.md
@$HOME/.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@/Users/herwy/dev/courtview/courtview.py
</context>

<tasks>

<task type="auto">
  <name>Task 1: Add archive tables to _init_db and exclude Racketeer from availability eviction</name>
  <files>/Users/herwy/dev/courtview/courtview.py</files>
  <action>
Two targeted edits to courtview.py:

**Edit A - Archive table creation in _init_db (after the stats_cache CREATE TABLE, before conn.commit()):**

Add three new CREATE TABLE IF NOT EXISTS statements:

- `archive_heatmap` - stores heatmap snapshots with full DOW x hour matrix per refresh:
  columns: `id INTEGER PRIMARY KEY AUTOINCREMENT, club_id TEXT, dow INTEGER, hour INTEGER,
  avg_occ REAL, samples INTEGER, hour_norm REAL, dow_norm REAL, captured_at INTEGER`
  No UNIQUE constraint - every refresh appends new rows.

- `archive_club_info` - stores club info payload snapshots:
  columns: `id INTEGER PRIMARY KEY AUTOINCREMENT, club_id TEXT, payload TEXT, captured_at INTEGER`

- `archive_financial` - stores financial/revenue payload snapshots keyed by club+window:
  columns: `id INTEGER PRIMARY KEY AUTOINCREMENT, club_id TEXT, start_time TEXT, end_time TEXT,
  payload TEXT, endpoint TEXT, captured_at INTEGER`
  (endpoint column stores "revenue-summary" or "payment-history" to distinguish the two financial calls)

**Edit B - Racketeer eviction guard in _refresh_loop:**

The constant `RACKETEER_CLUB_ID = "5111764d9bb14be3adbdb8e133e8bd80"` is already defined as
part of the HEATMAP_CLUBS list but not as a standalone constant. Define it near the top of the
constants section (after HEATMAP_CLUBS, before TPC constants):

```
RACKETEER_CLUB_ID = "5111764d9bb14be3adbdb8e133e8bd80"
```

In _refresh_loop, the availability rows are fetched with:
  `SELECT club_id, start_datetime, end_datetime FROM availability WHERE fetched_at > ?`

This query correctly only fetches non-stale rows for re-fetching - it does NOT delete anything.
The actual eviction risk is the TTL check in `get_cached`: rows older than CACHE_TTL return None
and get overwritten by a fresh fetch. Since `_refresh_loop` re-fetches every 6 hours and updates
`fetched_at`, Racketeer rows are never actually deleted. Verify this by reading `get_cached` and
confirming there is no DELETE statement for availability anywhere in the file.

If a DELETE does exist for availability outside of tests, add `AND club_id != ?` with
`(cutoff, RACKETEER_CLUB_ID)` to exclude Racketeer. If no DELETE exists, document this as
confirmed in the SUMMARY - eviction is passive (TTL miss) not active (DELETE), and the 6h
refresh loop effectively keeps Racketeer rows perpetually fresh, so no guard is needed.
Either way, add the `RACKETEER_CLUB_ID` constant for use by the archive writes.
  </action>
  <verify>
    <automated>grep -c "archive_heatmap\|archive_club_info\|archive_financial\|RACKETEER_CLUB_ID" /Users/herwy/dev/courtview/courtview.py</automated>
  </verify>
  <done>grep count >= 4 (at least one hit per archive table name plus the constant). All three CREATE TABLE statements present and RACKETEER_CLUB_ID defined.</done>
</task>

<task type="auto">
  <name>Task 2: Insert archive rows alongside existing live-table writes</name>
  <files>/Users/herwy/dev/courtview/courtview.py</files>
  <action>
Four targeted edits - each adds a plain INSERT (not INSERT OR REPLACE) to append to the archive
table immediately after the corresponding live-table write. Never modify the existing INSERT OR
REPLACE lines; only add new INSERTs after them.

**Archive point 1 - Heatmap (two locations):**

In `_fetch_heatmap_for_club` (around line 831) and in `_heatmap_refresh_loop` (around line 2166),
both contain an `INSERT OR REPLACE INTO heatmap_cache` followed by similar writes to
`heatmap_hour_signal` and `heatmap_dow_signal`.

After the `heatmap_cache` INSERT OR REPLACE block in each location, add:

```python
conn.execute(
    "INSERT INTO archive_heatmap (club_id, dow, hour, avg_occ, samples, captured_at)"
    " VALUES (?,?,?,?,?,?)",
    (club_id, dow, hour, avg_occ, samples, int(_now())),
)
```

This mirrors the data already being written to heatmap_cache. The `hour_norm` and `dow_norm`
columns can be omitted from the INSERT (they were included in the schema as optional context,
but the per-row values are already captured via the hour/dow signal tables). Keep the INSERT
simple: the columns listed above are sufficient.

Both INSERT OR REPLACE locations (initial fetch in `_fetch_heatmap_for_club` and the
background refresh in `_heatmap_refresh_loop`) need this archive write. Loop over the same
`(dow, hour, avg_occ, samples)` values that are being written to heatmap_cache.

**Archive point 2 - Club info (two locations):**

There are two club_info INSERT OR REPLACE locations:
- TPC path (around line 966): after `INSERT OR REPLACE INTO club_info_cache` for TPC club
- Padelmates path (around line 1436): after `INSERT OR REPLACE INTO club_info_cache` for
  Padelmates clubs

After each, add:
```python
conn.execute(
    "INSERT INTO archive_club_info (club_id, payload, captured_at) VALUES (?,?,?)",
    (club_id, payload, int(_now())),
)
```

**Archive point 3 - Financial data:**

In `api_revenue_summary` (around line 1729), after `set_stats_cached(ck, payload)` succeeds
(status 200), add a direct DB write:
```python
try:
    _conn = _db_connect()
    _conn.execute(
        "INSERT INTO archive_financial (club_id, start_time, end_time, payload, endpoint, captured_at)"
        " VALUES (?,?,?,?,?,?)",
        (club_id, start, end, payload, "revenue-summary", int(_now())),
    )
    _conn.commit()
    _conn.close()
except sqlite3.Error as exc:
    print(f"[archive] financial write error: {exc}")
```

Also add the same pattern in `api_payment_history` (find it by searching for
`/club/statistics/online_payment_history` or `payment-history`) after a successful 200 response
is obtained, using `endpoint="payment-history"`.

The archive write must be inside the `if r.status_code == 200:` guard so failed upstream
calls never produce archive rows.
  </action>
  <verify>
    <automated>grep -c "INSERT INTO archive_" /Users/herwy/dev/courtview/courtview.py</automated>
  </verify>
  <done>
grep count >= 5: heatmap archive (2 locations x 1 INSERT each = 2), club_info archive (2 locations = 2), financial archive (2 endpoints = 2). Minimum 5, expect 6. All INSERT INTO archive_ (not INSERT OR REPLACE).
  </done>
</task>

<task type="auto">
  <name>Task 3: Deploy and verify</name>
  <files>/Users/herwy/dev/courtview/courtview.py</files>
  <action>
1. Run cv-deploy to sync the modified courtview.py to the RPi.
2. Verify deployment with: `rpi 'grep -c "archive_heatmap" /root/projects/courtview/courtview.py'`
3. Restart gunicorn so the new archive tables are created by _init_db on startup:
   `rpi 'pkill -f "gunicorn.*courtview" && sleep 2 && cd /root/projects/courtview && gunicorn --daemon --workers 1 --bind 0.0.0.0:8766 --certfile /root/labs/certs/labs.doxx.crt --keyfile /root/labs/certs/labs.doxx.key --error-logfile courtview.log courtview:app'`
4. Verify the archive tables were created in the DB:
   `rpi 'sqlite3 /root/projects/courtview/courtview_cache.db ".tables"'`
   Expected output includes: archive_club_info, archive_financial, archive_heatmap
5. Verify gunicorn is still running after restart:
   `rpi 'pgrep -fa "gunicorn.*courtview"'`
  </action>
  <verify>
    <automated>rpi 'grep -c "archive_heatmap" /root/projects/courtview/courtview.py'</automated>
  </verify>
  <done>grep count > 0, sqlite3 .tables output includes all three archive_ tables, pgrep shows gunicorn running.</done>
</task>

</tasks>

<threat_model>
## Trust Boundaries

| Boundary | Description |
|----------|-------------|
| Archive writes | Append-only; no new inputs from untrusted sources; all data originates from upstream API responses already validated by existing code |

## STRIDE Threat Register

| Threat ID | Category | Component | Disposition | Mitigation Plan |
|-----------|----------|-----------|-------------|-----------------|
| T-vxi-01 | Information Disclosure | archive_financial table | accept | Financial data is already stored in stats_cache; archive adds persistence, same exposure surface |
| T-vxi-02 | Denial of Service | archive_heatmap unbounded growth | mitigate | Archive rows accumulate indefinitely - acceptable for research use; add periodic `DELETE FROM archive_heatmap WHERE captured_at < strftime('%s','now') - 365*86400` in a future cleanup pass if needed |
| T-vxi-SC | Tampering | No new package installs in this task | accept | No npm/pip/cargo installs; slopcheck not required |
</threat_model>

<verification>
- `rpi 'sqlite3 /root/projects/courtview/courtview_cache.db ".tables"'` lists archive_club_info, archive_financial, archive_heatmap
- `grep -c "INSERT INTO archive_" /Users/herwy/dev/courtview/courtview.py` returns >= 5
- `grep -c "RACKETEER_CLUB_ID" /Users/herwy/dev/courtview/courtview.py` returns >= 1
- gunicorn process running on RPi after restart
</verification>

<success_criteria>
- Three archive tables exist in the live SQLite DB on the RPi
- Racketeer availability is confirmed never-deleted (either by guard or by verification that no DELETE targets availability)
- All heatmap, club_info, and financial writes append to archive tables on every refresh cycle
- No existing INSERT OR REPLACE lines were removed or modified
</success_criteria>

<output>
Create `.planning/quick/260612-vxi-archive-racketeer-data-permanently-for-o/260612-vxi-SUMMARY.md` when done
</output>
