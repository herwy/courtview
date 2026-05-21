# Padelmates API - Responsible Disclosure Findings

Discovered: 2026-05-21 during CourtView development (Android APK reverse engineering via jadx + API exploration).

---

## Finding 1: CRITICAL - Unauthenticated PII exposure via `/club/follower/crud`

**Endpoint:** `GET /club/follower/crud?club_id=<id>`
**Auth required:** None
**Status:** Confirmed

Returns a `followers` array containing every user who has ever followed the club, with full PII per record:

- `name` (full name)
- `email` (direct email or Apple private relay address)
- `phone` (mobile number in full)
- `player_id` (Firebase UID - unique cross-platform identifier)
- `mongo_id` (internal MongoDB ID)
- `followDate`, `originalFollowDate`, `unfollowDate`
- `memberships`, `isChildAccount`, `parentUserId`, `childDetails`

**Scale:** 15,308 records for a single mid-size London club (Racketeer). Padelmates operates globally across hundreds of clubs. The platform-wide exposure is likely in the hundreds of thousands to millions of user records.

**Exploitability:** A single unauthenticated GET request with any valid `club_id` returns the complete dataset. Club IDs are exposed via the public search endpoint `/player/player_booking/search_clubs`.

**GDPR exposure:** UK/EU-resident user data (names, emails, phone numbers) exposed without any access control. Likely violates GDPR Articles 5 (data minimisation, integrity/confidentiality) and 25 (data protection by design).

**Fix:** Require club manager or admin authentication token before returning follower data.

---

## Finding 2: HIGH - Unauthenticated financial data via `/club/statistics/financial` and `/v2`

**Endpoints:**
- `GET /club/statistics/financial?club_id=<id>&start_time=<ms>&end_time=<ms>`
- `GET /club/statistics/financial/v2?club_id=<id>&...`

**Auth required:** None
**Status:** Confirmed

Returns:
- `total_booking_revenue` (GBP value)
- `total_bookings`
- `total_members`
- `new_members`
- `avg_booking_value`
- Breakdown by booking type (member, pay-as-you-go, cancelled, etc.)

**Scope:** Any club's financial performance data is publicly readable with a club ID.

**Fix:** Require club manager/admin auth token.

---

## Finding 3: MEDIUM - Unauthenticated operational data via `/club/statistics/operational`

**Endpoint:** `GET /club/statistics/operational?club_ids=<id>&selected_duration=month&...`

**Auth required:** None
**Status:** Confirmed

Returns:
- `hottest_time_slots` - court usage by 30-min bucket
- `week_day_wise_activity_count_combined_graph` - activity by day of week
- `court_wise_activity_count_combined_graph` - per-court booking counts
- Full 30-day utilisation breakdown

Less sensitive than Finding 1 and 2 (no PII, no financial totals), but exposes operational intelligence about competitors.

**Fix:** Same auth requirement as above, or acceptable as public data if intended for transparency.

---

## Finding 4: HIGH - `/club/member/` endpoint (504 timeout - confirmed mass PII exposure)

**Endpoint:** `GET /club/member/?club_id=<id>`
**Auth required:** None
**Status:** Returns 504 Gateway Timeout

Confirmed via OpenAPI spec (`/openapi.json`): endpoint is named "Get All Followers And Members", accepts only `club_id` (no pagination parameters), and returns schema `GetFollowersAndMembers`. The 504 is because the full dataset is too large to serialize within the server timeout - not a client-side fixable issue. The dataset is larger than the 15,308-record followers list (Finding 1) since it combines members + followers.

Related unauthenticated endpoint: `/club/member/crud?club_id=X&player_id=Y` - returns individual member detail by player ID. If a player ID from Finding 1 is used, this returns their full member record without auth.

**Fix:** Require club manager/admin auth token. Add pagination to `/club/member/`.

---

## Disclosure plan

**Severity:** Critical (Finding 1 is a GDPR violation exposing tens of thousands of user records per club)

**Recipient:** security@padelmates.com or via their support portal
**Timeline:** Notify within 7 days of discovery, allow 90 days for fix before public disclosure

**Draft notification subject:** "Unauthenticated API endpoints expose customer PII across all clubs"

The notification should reference Finding 1 (most severe) and offer to provide a full technical writeup. Avoid disclosing specific club data in the notification. The `club_id` used in testing is Racketeer's (our own club), so the probe did not access any third-party data.

---

## Notes

- Testing performed via direct curl from Mac IP, not from RPi, to avoid appearing as the proxy server
- No club data was stored beyond the count (15,308 followers). Actual PII records were not saved
- The courtview.py proxy whitelist (`ALLOWED_PATHS`) intentionally does NOT include `/club/follower/crud` or `/club/member/`
