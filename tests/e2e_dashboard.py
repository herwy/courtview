#!/usr/bin/env python3
"""Playwright network-capture check of the CourtView dashboard.

Usage: CV_TOKEN=... python3 tests/e2e_dashboard.py [base_url]
Scenario R: Racketeer selected, every tab visited, no live Racketeer request.
Scenario P: Padium selected, Revenue payment history paging, anonymous top spenders and Best Times.
Both scenarios count email and UK mobile pattern matches over every /api/ JSON body (must be 0).
The token is only ever set as a cookie; it is never printed.
"""
import os
import re
import sys
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

RACKETEER = "5111764d9bb14be3adbdb8e133e8bd80"
PADIUM = "47d2eb0db7194a9dbd29783c3a2a82ad"
DEFAULT_BASE = "https://courtview.labs.doxx:47200"
WARMUP_WAIT_MS = 8000
RECS_WAIT_MS = 30000
TABS = ["book", "club", "stats", "compare", "revenue"]
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")

EMAIL_RE = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
MOBILE_RE = re.compile(r"(?<![0-9A-Za-z])(?:\+44 ?\(?0?\)?|0)7\d{3} ?\d{3} ?\d{3}(?![0-9A-Za-z])")
PAY_HEADERS = ["Date", "Description", "Amount", "Status"]
LEADER_RE = re.compile(r"^\d\. Customer #[0-9A-F]{4}")
scan = {"bodies": 0, "email": 0, "mobile": 0}

failures = 0


def check(name, ok, detail=""):
    global failures
    if not ok:
        failures += 1
    print(f"{'PASS' if ok else 'FAIL'}: {name}{(' - ' + detail) if detail and not ok else ''}")


def scan_body(resp):
    """Count PII-pattern matches in /api/ JSON bodies; never keeps or prints a body."""
    try:
        if "/api/" not in resp.url or "json" not in (resp.headers.get("content-type") or ""):
            return
        text = resp.text()
    except Exception:
        return
    scan["bodies"] += 1
    scan["email"] += len(EMAIL_RE.findall(text))
    scan["mobile"] += len(MOBILE_RE.findall(text))


def new_page(browser, base, club_id, token):
    ctx = browser.new_context(ignore_https_errors=True)
    host = urlparse(base).hostname
    ctx.add_cookies([{"name": "courtview_token", "value": token, "domain": host, "path": "/"}])
    ctx.add_init_script(f"localStorage.setItem('cv-club-id', '{club_id}')")
    page = ctx.new_page()
    reqs, statuses = [], []
    page.on("request", lambda r: reqs.append(r.url))
    page.on("response", lambda r: statuses.append((r.status, r.url)))
    page.on("response", scan_body)
    return ctx, page, reqs, statuses


def open_tab(page, tab):
    page.click(f"#tab-{tab}-btn")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(500)


def scenario_racketeer(browser, base, token):
    print("--- Scenario R (Racketeer)")
    ctx, page, reqs, statuses = new_page(browser, base, RACKETEER, token)
    page.goto(base + "/")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(WARMUP_WAIT_MS)
    for tab in TABS:
        open_tab(page, tab)
    page.wait_for_timeout(1500)

    page.wait_for_selector("#revPayOut table", timeout=30000)
    headers = page.eval_on_selector_all("#revPayOut thead th", "els => els.map(e => e.textContent.trim())")
    check("Racketeer archive payment table headers", headers == PAY_HEADERS, str(headers))
    check("no payment search box", page.locator("#revPaySearch").count() == 0)

    live = [u for u in reqs if RACKETEER in u and not urlparse(u).path.startswith("/api/archive/")]
    check(f"no non-archive Racketeer requests (count={len(live)})", not live, "; ".join(live[:3]))
    gone = [u for s, u in statuses if s == 410]
    check(f"no 410 responses (count={len(gone)})", not gone, "; ".join(gone[:3]))

    grid = page.inner_text("#gridOut")
    check("availability shows stopped-taking-bookings message", "stopped taking bookings" in grid)
    live_opts = page.eval_on_selector_all(
        ".archive-selector select option", "els => els.filter(e => e.textContent.trim() === 'Live').length")
    check("no archive select has a Live option", live_opts == 0)
    n_selectors = page.eval_on_selector_all(".archive-selector", "els => els.length")
    check("three archive selectors present", n_selectors == 3, str(n_selectors))
    check("revenue payment table rendered", page.locator("#revPayOut table").count() == 1)
    check("revenue Load More hidden", page.eval_on_selector("#revLoadMoreWrap", "e => e.hidden"))
    compare = page.inner_text("#compareOut")
    check("compare has no Racketeer row", "Racketeer" not in compare)
    check("compare lists Padium and Rocket", "Padium" in compare and "Rocket" in compare)
    ctx.close()


def scenario_padium(browser, base, token):
    print("--- Scenario P (Padium)")
    ctx, page, reqs, statuses = new_page(browser, base, PADIUM, token)
    page.goto(base + "/")
    page.wait_for_load_state("networkidle")
    open_tab(page, "revenue")
    page.wait_for_selector("#revPayOut table, #revPayOut .empty", timeout=60000)
    page.wait_for_function(
        "() => !document.getElementById('revLoadMore').disabled", timeout=60000)

    dates = page.eval_on_selector_all(
        "#revPayOut tbody tr td:first-child", "els => els.map(e => e.textContent.trim())")
    check(f"payment rows present (count={len(dates)})", len(dates) > 0)
    check("every Date cell is YYYY-MM-DD HH:MM", all(DATE_RE.match(d) for d in dates))
    check("rows sorted newest first", dates == sorted(dates, reverse=True))
    meta = page.inner_text("#revPayMeta")
    check("pay meta shows 'loaded · back to'", "loaded · back to" in meta, meta)

    if page.is_visible("#revLoadMoreWrap"):
        before_rows = len(dates)
        before_meta = meta
        page.click("#revLoadMore")
        page.wait_for_timeout(500)
        page.wait_for_function(
            "() => !document.getElementById('revLoadMore').disabled", timeout=60000)
        after_rows = page.locator("#revPayOut tbody tr").count()
        after_meta = page.inner_text("#revPayMeta")
        check("Load More grows rows or moves 'back to' day earlier",
              after_rows > before_rows or after_meta != before_meta,
              f"{before_rows}->{after_rows} '{before_meta}' -> '{after_meta}'")
    else:
        print("SKIP: Load More not visible (month fully loaded)")

    page.wait_for_function(
        "() => !document.getElementById('revLoadMore').disabled", timeout=60000)
    headers = page.eval_on_selector_all("#revPayOut thead th", "els => els.map(e => e.textContent.trim())")
    check("payment table headers are Date, Description, Amount, Status", headers == PAY_HEADERS, str(headers))
    check("no payment search box", page.locator("#revPaySearch").count() == 0)
    leaders = page.eval_on_selector_all("#revLeadersOut .cp-name", "els => els.map(e => e.textContent.trim())")
    check(f"top spenders are anonymous (count={len(leaders)})",
          len(leaders) > 0 and all(LEADER_RE.match(t) for t in leaders))

    open_tab(page, "club")
    open_tab(page, "stats")
    check("club info shows no email or phone rows",
          "Email" not in page.inner_text("#clubOut") and "Phone" not in page.inner_text("#clubOut"))
    open_tab(page, "book")

    page.wait_for_timeout(RECS_WAIT_MS)
    open_tab(page, "book")
    rec = page.inner_text("#recOut")
    check("Best Times not stuck on 'Loading recommendations...'", "Loading recommendations..." not in rec, rec[:60])
    ctx.close()


def main():
    base = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_BASE
    token = os.environ.get("CV_TOKEN", "")
    if not token:
        print("CV_TOKEN env var is required")
        return 2
    with sync_playwright() as p:
        browser = p.chromium.launch()
        scenario_racketeer(browser, base, token)
        scenario_padium(browser, base, token)
        browser.close()
    check(f"no email or UK mobile pattern in /api JSON bodies (bodies={scan['bodies']}, "
          f"email={scan['email']}, mobile={scan['mobile']})",
          scan["bodies"] > 0 and scan["email"] == 0 and scan["mobile"] == 0)
    print(f"RESULT: {'FAILED (' + str(failures) + ')' if failures else 'ALL PASS'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
