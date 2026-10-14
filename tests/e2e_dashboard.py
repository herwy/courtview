#!/usr/bin/env python3
"""Playwright network-capture check of the CourtView dashboard.

Usage: CV_TOKEN=... python3 tests/e2e_dashboard.py [base_url]
Scenario R: Racketeer selected, every tab visited, no live Racketeer request.
Scenario P: Padium selected, Revenue payment history paging and Best Times.
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

failures = 0


def check(name, ok, detail=""):
    global failures
    if not ok:
        failures += 1
    print(f"{'PASS' if ok else 'FAIL'}: {name}{(' - ' + detail) if detail and not ok else ''}")


def new_page(browser, base, club_id, token):
    ctx = browser.new_context(ignore_https_errors=True)
    host = urlparse(base).hostname
    ctx.add_cookies([{"name": "courtview_token", "value": token, "domain": host, "path": "/"}])
    ctx.add_init_script(f"localStorage.setItem('cv-club-id', '{club_id}')")
    page = ctx.new_page()
    reqs, statuses = [], []
    page.on("request", lambda r: reqs.append(r.url))
    page.on("response", lambda r: statuses.append((r.status, r.url)))
    return ctx, page, reqs, statuses


def open_tab(page, tab):
    page.click(f"#tab-{tab}-btn")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(500)


def search_token(page):
    """Last whitespace token (len >= 3) of the first row's Player cell, or None."""
    name = page.eval_on_selector(
        "#revPayOut tbody tr td:nth-child(2)", "e => e.textContent.trim()")
    toks = [t for t in name.split() if len(t) >= 3]
    return toks[-1] if toks else None


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
    tok = search_token(page)
    if tok:
        page.fill("#revPaySearch", tok)
        page.wait_for_function(
            "() => document.getElementById('revPayMeta').textContent.includes('match')", timeout=30000)
        check("Racketeer archive search shows matches", "match" in page.inner_text("#revPayMeta"))
        page.fill("#revPaySearch", "")
        page.wait_for_function(
            "() => document.getElementById('revPayMeta').textContent.includes('archived')", timeout=30000)
        check("clearing Racketeer search restores archived meta", "archived" in page.inner_text("#revPayMeta"))
    else:
        print("SKIP: no searchable player token in Racketeer archive")

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
    tok = search_token(page)
    if tok:
        page.fill("#revPaySearch", tok)
        page.wait_for_function(
            "() => /\\d+ match(es)? in /.test(document.getElementById('revPayMeta').textContent)", timeout=90000)
        rows = page.eval_on_selector_all(
            "#revPayOut tbody tr",
            "els => els.map(e => [e.children[0].textContent.trim(), e.children[1].textContent, e.children[2].textContent])")
        low = tok.lower()
        check(f"search returned rows (count={len(rows)})", len(rows) > 0)
        check("every result matches the token", all(low in (r[1] + r[2]).lower() for r in rows))
        check("search results newest first", [r[0] for r in rows] == sorted([r[0] for r in rows], reverse=True))
        check("Load More hidden during search", page.eval_on_selector("#revLoadMoreWrap", "e => e.hidden"))
        page.fill("#revPaySearch", tok[0])
        page.wait_for_timeout(1000)
        one = page.inner_text("#revPayMeta")
        check("single character does not leave 'Searching...'", "Searching" not in one, one)
        page.fill("#revPaySearch", "")
        page.wait_for_function(
            "() => document.getElementById('revPayMeta').textContent.includes('loaded · back to')", timeout=30000)
        check("clearing search restores paged view",
              page.locator("#revPayOut tbody tr").count() > 0, page.inner_text("#revPayMeta"))
    else:
        print("SKIP: no searchable player token in Padium rows")

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
    print(f"RESULT: {'FAILED (' + str(failures) + ')' if failures else 'ALL PASS'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
