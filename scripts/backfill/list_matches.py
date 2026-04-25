"""Scrape the OddsPortal ``/results/`` pages of one league-season and emit
all per-match URLs to stdout (or a file via ``--out``).

This is a lightweight one-off per season; its output feeds
``scripts/backfill/orchestrator.py``. Keeping it out of the main scraper flow
avoids paying the full Playwright boot every time we want the season's URL list.

OddsPortal paginates the results via an SPA control: clicking
``a.pagination-link`` with text "Next" triggers an XHR
(``/ajax-sport-country-tournament-archive_/...``) that replaces the visible
event rows and updates the URL fragment to ``#/page/N/``. There is no numeric
paginator anchor — only "Next". We drive the pagination by clicking Next until
either the button disappears or the URL fragment stops advancing.

Usage
-----
    uv run python -m scripts.backfill.list_matches \
        -l england-premier-league --season 2024-2025 \
        --out data/matches-england-premier-league-2024-2025.txt
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
import time
from pathlib import Path

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError, async_playwright

from oddsharvester.core.url_builder import URLBuilder
from oddsharvester.utils.constants import ODDSPORTAL_BASE_URL

# OddsPortal's edge returns 503 to any browser-class User-Agent from AWS IPs;
# curl-style UA passes through while navigator.userAgent stays HeadlessChrome
# (so the SPA renders for us).
UA_HEADERS = {"User-Agent": "curl/7.88.1"}

MAX_PAGES = 30  # safety cap; EPL season is ~8 pages, other leagues up to ~12
POST_GOTO_WAIT_MS = 3000
BETWEEN_PAGE_WAIT_MS = 4500

logger = logging.getLogger("backfill.list_matches")

_PAGE_HASH_RE = re.compile(r"#/page/(\d+)/")


async def _accept_cookies(page: Page) -> None:
    try:
        btn = page.locator("#onetrust-accept-btn-handler")
        if await btn.count() > 0 and await btn.first.is_visible():
            await btn.first.click()
            await page.wait_for_timeout(2000)
    except Exception as err:  # pragma: no cover - best-effort
        logger.debug("cookie-accept skipped: %s", err)


async def _collect_match_urls(page: Page) -> list[str]:
    """Extract deduplicated /h2h/ links whose row class *starts* with ``eventRow``.

    A looser ``[class*='eventRow']`` also matches sidebar widgets (``nextEventRow``,
    ``upcomingEventRow``) which have been observed to contaminate the listing with
    cross-league matches.
    """
    hrefs: list[str] = await page.evaluate(
        """
        () => {
            const rows = Array.from(document.querySelectorAll("[class]")).filter(el => {
                const cls = (el.getAttribute('class') || '').split(/\\s+/);
                return cls.some(c => c.startsWith('eventRow'));
            });
            const urls = new Set();
            rows.forEach(row => {
                row.querySelectorAll("a[href*='/football/h2h/']").forEach(a => {
                    const href = a.getAttribute("href");
                    if (href) urls.add(href);
                });
            });
            return Array.from(urls);
        }
        """
    )
    absolute = []
    for href in hrefs:
        if href.startswith("http"):
            absolute.append(href)
        else:
            absolute.append(f"{ODDSPORTAL_BASE_URL}{href}")
    return absolute


async def _current_page_number(page: Page) -> int:
    match = _PAGE_HASH_RE.search(page.url)
    return int(match.group(1)) if match else 1


async def _click_next(page: Page) -> bool:
    """Click the paginator "Next" anchor; return True on success.

    The anchor sits well below the fold; scroll-into-view is required before the
    click will register.
    """
    next_locator = page.locator("a.pagination-link", has_text="Next")
    count = await next_locator.count()
    if count == 0:
        logger.debug("no a.pagination-link[Next] present — end of pagination")
        return False
    first = next_locator.first
    try:
        await first.scroll_into_view_if_needed(timeout=5000)
        await first.click(timeout=5000)
        return True
    except PlaywrightTimeoutError as err:
        logger.warning("Next click timed out: %s", err)
        return False


async def _paginate(page: Page) -> list[str]:
    """Click Next repeatedly and aggregate event-row URLs from each page.

    Returns the deduplicated (first-seen order) list of match URLs.
    """
    all_urls: list[str] = []
    seen: set[str] = set()

    for iteration in range(1, MAX_PAGES + 1):
        page_num = await _current_page_number(page)
        await page.wait_for_timeout(BETWEEN_PAGE_WAIT_MS)
        urls = await _collect_match_urls(page)
        new_count = 0
        for url in urls:
            if url not in seen:
                seen.add(url)
                all_urls.append(url)
                new_count += 1
        logger.info("page %d (iter %d): +%d new match URLs (%d visible on page, %d cumulative)", page_num, iteration, new_count, len(urls), len(all_urls))

        # Snapshot first row text so we can detect a real DOM swap after the click.
        prev_first_row = await page.evaluate(
            """
            () => {
                const r = document.querySelector("[class^='eventRow']");
                return r ? (r.innerText || '').substring(0, 200) : null;
            }
            """
        )

        if not await _click_next(page):
            break

        # Wait for the URL fragment to advance OR for rows to change.
        try:
            await page.wait_for_url(
                lambda u, _prev=page_num: (
                    bool(_PAGE_HASH_RE.search(u)) and int(_PAGE_HASH_RE.search(u).group(1)) > _prev
                ),
                timeout=15000,
            )
        except PlaywrightTimeoutError:
            logger.warning("URL did not advance after Next click at page %d — stopping", page_num)
            break

        # OddsPortal swaps the DOM *after* the URL changes; poll up to 12s for the
        # first event row's text to differ from the pre-click snapshot.
        for _ in range(12):
            await page.wait_for_timeout(1000)
            current_first_row = await page.evaluate(
                """
                () => {
                    const r = document.querySelector("[class^='eventRow']");
                    return r ? (r.innerText || '').substring(0, 200) : null;
                }
                """
            )
            if current_first_row and current_first_row != prev_first_row:
                break
        else:
            logger.warning("DOM did not refresh within 12s after URL advance at page %d", page_num)

    return all_urls


def _upload_to_s3(bucket: str, prefix: str, sport: str, league: str, season: str, content: str) -> str:
    """Upload the match list to S3 under ``_bootstrap/oddsportal/<sport>/<league>/<season>/matches.txt``.

    The bootstrap prefix is kept separate from the per-match JSONs so it does not get
    picked up by ``list_existing_hashes`` in the orchestrator. Returns the full S3 URI.
    """
    import boto3

    cleaned_prefix = prefix.strip("/")
    key = f"{cleaned_prefix}/{sport}/{league}/{season}/matches.txt"
    s3 = boto3.client("s3")
    s3.put_object(Bucket=bucket, Key=key, Body=content.encode("utf-8"), ContentType="text/plain")
    return f"s3://{bucket}/{key}"


def _parse_proxy_url(proxy_url: str) -> dict[str, str]:
    """Parse a ``http://user:pass@host:port`` URL into Playwright's proxy dict."""
    from urllib.parse import urlparse

    parsed = urlparse(proxy_url)
    server = f"{parsed.scheme}://{parsed.hostname}"
    if parsed.port:
        server += f":{parsed.port}"
    out = {"server": server}
    if parsed.username:
        out["username"] = parsed.username
    if parsed.password:
        out["password"] = parsed.password
    return out


async def run(
    sport: str,
    league: str,
    season: str,
    out: Path | None,
    s3_bucket: str | None,
    s3_prefix: str,
    proxy_url: str | None = None,
) -> int:
    base_url = URLBuilder.get_historic_matches_url(sport=sport, league=league, season=season)
    logger.info("base results URL: %s", base_url)
    if proxy_url:
        logger.info("using proxy: %s", proxy_url.split("@")[-1] if "@" in proxy_url else proxy_url)
    started = time.time()

    async with async_playwright() as pw:
        launch_kwargs: dict = {"headless": True, "args": ["--no-sandbox"]}
        if proxy_url:
            launch_kwargs["proxy"] = _parse_proxy_url(proxy_url)
        browser = await pw.chromium.launch(**launch_kwargs)
        context = await browser.new_context(
            viewport={"width": 1600, "height": 1100},
            extra_http_headers=UA_HEADERS,
        )
        page = await context.new_page()
        try:
            await page.goto(base_url, wait_until="networkidle", timeout=60000)
            await _accept_cookies(page)
            await page.wait_for_timeout(POST_GOTO_WAIT_MS)
            logger.info(
                "initial html=%dB eventRows=%d",
                len(await page.content()),
                await page.locator("[class^='eventRow']").count(),
            )
            urls = await _paginate(page)
        finally:
            await browser.close()

    if not urls:
        logger.error("no match URLs found — check league/season parameters")
        return 0

    payload = "\n".join(urls) + "\n"
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(payload)
        logger.info("wrote %d URLs to %s", len(urls), out)
    else:
        sys.stdout.write(payload)

    if s3_bucket:
        try:
            s3_uri = _upload_to_s3(s3_bucket, s3_prefix, sport, league, season, payload)
            logger.info("mirrored match list to %s", s3_uri)
        except Exception as err:  # pragma: no cover - best-effort durability
            logger.error("S3 upload failed: %s", err)

    logger.info("done in %.1fs (%d URLs)", time.time() - started, len(urls))
    return len(urls)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-s", "--sport", default="football")
    parser.add_argument("-l", "--league", required=True, help="league slug (e.g. england-premier-league)")
    parser.add_argument("--season", required=True, help="'YYYY-YYYY' or 'YYYY'")
    parser.add_argument("--out", type=Path, default=None, help="write URLs to this file instead of stdout")
    parser.add_argument(
        "--s3-bucket",
        default=None,
        help="also upload the match list to s3://<bucket>/<s3-prefix>/<sport>/<league>/<season>/matches.txt",
    )
    parser.add_argument(
        "--s3-prefix",
        default="_bootstrap/oddsportal",
        help="prefix under --s3-bucket (default: _bootstrap/oddsportal)",
    )
    parser.add_argument(
        "--proxy-url",
        default=None,
        help="route Playwright through this HTTP(S) proxy (e.g. http://USER:PASS@geo.iproyal.com:12321)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s %(message)s",
    )
    count = asyncio.run(
        run(args.sport, args.league, args.season, args.out, args.s3_bucket, args.s3_prefix, args.proxy_url)
    )
    return 0 if count > 0 else 1


if __name__ == "__main__":  # pragma: no cover - CLI entry
    sys.exit(main())
