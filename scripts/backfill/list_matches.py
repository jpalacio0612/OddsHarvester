"""Scrape the OddsPortal ``/results/`` pages of one league-season and emit
all per-match URLs to stdout (or a file via ``--out``).

This is a lightweight one-off per season; its output feeds
``scripts/backfill/orchestrator.py``. Keeping it out of the main scraper flow
avoids paying the full Playwright boot every time we want the season's URL list.

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

from playwright.async_api import async_playwright

from oddsharvester.core.url_builder import URLBuilder
from oddsharvester.utils.constants import ODDSPORTAL_BASE_URL

SCROLL_STEPS = 8
SCROLL_WAIT_MS = 1000
EXTRA_SETTLE_MS = 2000

logger = logging.getLogger("backfill.list_matches")


async def _accept_cookies(page) -> None:
    try:
        btn = page.locator("#onetrust-accept-btn-handler")
        if await btn.count() > 0 and await btn.first.is_visible():
            await btn.first.click()
            await page.wait_for_timeout(2000)
    except Exception as err:  # pragma: no cover - best-effort
        logger.debug("cookie-accept skipped: %s", err)


async def _scroll_to_load_rows(page) -> None:
    """Lazy-load rows by incremental scrolling until no new content appears."""
    for i in range(SCROLL_STEPS):
        await page.mouse.wheel(0, 2000)
        await page.wait_for_timeout(SCROLL_WAIT_MS)
        if i % 2 == 1:
            row_count = await page.locator("[class*='eventRow']").count()
            logger.debug("scroll step %d: %d eventRows", i + 1, row_count)
    await page.wait_for_timeout(EXTRA_SETTLE_MS)


async def _collect_match_urls(page) -> list[str]:
    """Extract deduplicated /h2h/ links from the results page."""
    hrefs: list[str] = await page.evaluate(
        """
        () => {
            const rows = Array.from(document.querySelectorAll("[class*='eventRow']"));
            const urls = new Set();
            rows.forEach(row => {
                row.querySelectorAll("a[href*='/h2h/']").forEach(a => {
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


async def _paginate(page, base_url: str) -> list[str]:
    """Walk every numbered results page and aggregate match URLs.

    OddsPortal's ``/results/`` paginator exposes numeric ``a.pagination-link``
    anchors; we iterate the distinct page numbers in ascending order and append
    each page's match URLs. The first page is the base URL (``#/page/1/``).
    """
    await _scroll_to_load_rows(page)
    first_page = await _collect_match_urls(page)
    logger.info("page 1: %d matches", len(first_page))

    # Find all numeric pagination links
    pagination_numbers = await page.evaluate(
        """
        () => {
            const links = Array.from(document.querySelectorAll("a.pagination-link"));
            const nums = new Set();
            links.forEach(a => {
                const txt = (a.innerText || "").trim();
                if (/^\\d+$/.test(txt)) nums.add(parseInt(txt, 10));
            });
            return Array.from(nums).sort((a, b) => a - b);
        }
        """
    )
    logger.info("pagination pages discovered: %s", pagination_numbers)

    all_urls = list(first_page)
    for page_num in pagination_numbers:
        if page_num <= 1:
            continue
        page_url = f"{base_url}#/page/{page_num}/"
        logger.info("navigating to page %d: %s", page_num, page_url)
        await page.goto(page_url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(3000)
        await _scroll_to_load_rows(page)
        page_urls = await _collect_match_urls(page)
        logger.info("page %d: %d matches", page_num, len(page_urls))
        all_urls.extend(page_urls)

    # Dedupe preserving first-seen order
    seen: set[str] = set()
    deduped: list[str] = []
    for url in all_urls:
        if url in seen:
            continue
        seen.add(url)
        deduped.append(url)
    return deduped


async def run(sport: str, league: str, season: str, out: Path | None) -> int:
    base_url = URLBuilder.get_historic_matches_url(sport=sport, league=league, season=season)
    logger.info("base results URL: %s", base_url)
    started = time.time()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(viewport={"width": 1600, "height": 1100})
        page = await context.new_page()
        try:
            await page.goto(base_url, wait_until="domcontentloaded", timeout=60000)
            await _accept_cookies(page)
            urls = await _paginate(page, base_url)
        finally:
            await browser.close()

    if not urls:
        logger.error("no match URLs found — check league/season parameters")
        return 0

    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(urls) + "\n")
        logger.info("wrote %d URLs to %s", len(urls), out)
    else:
        for url in urls:
            sys.stdout.write(url + "\n")

    logger.info("done in %.1fs (%d URLs)", time.time() - started, len(urls))
    return len(urls)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-s", "--sport", default="football")
    parser.add_argument("-l", "--league", required=True, help="league slug (e.g. england-premier-league)")
    parser.add_argument("--season", required=True, help="'YYYY-YYYY' or 'YYYY'")
    parser.add_argument("--out", type=Path, default=None, help="write URLs to this file instead of stdout")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s %(message)s",
    )
    count = asyncio.run(run(args.sport, args.league, args.season, args.out))
    return 0 if count > 0 else 1


if __name__ == "__main__":  # pragma: no cover - CLI entry
    sys.exit(main())
