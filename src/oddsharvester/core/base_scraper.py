import asyncio
from datetime import UTC, date, datetime, timedelta
from enum import Enum
import json
import logging
import random
import re
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bs4 import BeautifulSoup
from playwright.async_api import Page, TimeoutError

from oddsharvester.core.browser_helper import BrowserHelper
from oddsharvester.core.odds_portal_market_extractor import OddsPortalMarketExtractor
from oddsharvester.core.odds_portal_selectors import OddsPortalSelectors
from oddsharvester.core.playwright_manager import PlaywrightManager
from oddsharvester.core.retry import RetryConfig, classify_error, is_retryable_error, retry_with_backoff
from oddsharvester.core.scrape_result import FailedUrl, ScrapeResult, ScrapeStats
from oddsharvester.utils.bookies_filter_enum import BookiesFilter
from oddsharvester.utils.constants import (
    DEFAULT_REQUEST_DELAY_S,
    DYNAMIC_CONTENT_WAIT_MS,
    MATCH_RETRY_BASE_DELAY,
    MATCH_RETRY_MAX_ATTEMPTS,
    MATCH_RETRY_MAX_DELAY,
    NAVIGATION_TIMEOUT_MS,
    ODDS_FORMAT_SELECTOR_TIMEOUT_MS,
    ODDS_FORMAT_WAIT_MS,
    ODDSPORTAL_BASE_URL,
    REQUEST_DELAY_JITTER_FACTOR,
    SELECTOR_TIMEOUT_MS,
)
from oddsharvester.utils.odds_format_enum import OddsFormat
from oddsharvester.utils.utils import clean_html_text

_MONTH_ABBREV_TO_NUM = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


def _parse_date_header(header_text: str, tz_name: str | None = None) -> date | None:
    """
    Parse an OddsPortal date-header string into a date object.

    Handles the formats observed on oddsportal.com listing pages:
        - "Today, 14 Apr"          -> today in the reference timezone
        - "Tomorrow, 15 Apr"       -> tomorrow in the reference timezone
        - "Yesterday, 13 Apr"      -> yesterday in the reference timezone
        - "18 Apr 2026"            -> explicit date
        - "Today, 14 Apr  - Apertura" -> tournament suffix is stripped

    When "Today"/"Tomorrow" is present it is trusted over the day/month tokens,
    since OddsPortal resolves them based on the browser timezone.

    Args:
        header_text: Raw inner text of the [data-testid='date-header'] element.
        tz_name: IANA timezone name used to resolve "Today"/"Tomorrow" and to
            infer missing years. Defaults to UTC.

    Returns:
        A date object, or None if the input cannot be parsed (fail-safe: callers
        should treat None as "do not filter").
    """
    if not header_text:
        return None

    text = header_text.strip()
    if " - " in text:
        text = text.split(" - ", 1)[0].strip()

    try:
        tz = ZoneInfo(tz_name) if tz_name else ZoneInfo("UTC")
    except (ZoneInfoNotFoundError, ValueError):
        tz = ZoneInfo("UTC")

    now_date = datetime.now(tz).date()

    lower = text.lower()
    if lower.startswith("today"):
        return now_date
    if lower.startswith("tomorrow"):
        return now_date + timedelta(days=1)
    if lower.startswith("yesterday"):
        return now_date - timedelta(days=1)

    parts = text.split()

    if len(parts) == 3:
        day_str, month_str, year_str = parts
        try:
            day = int(day_str)
            month = _MONTH_ABBREV_TO_NUM.get(month_str[:3].lower())
            year = int(year_str)
            if month is None:
                return None
            return date(year, month, day)
        except (ValueError, TypeError):
            return None

    if len(parts) == 2:
        day_str, month_str = parts
        try:
            day = int(day_str)
            month = _MONTH_ABBREV_TO_NUM.get(month_str[:3].lower())
            if month is None:
                return None
            candidate = date(now_date.year, month, day)
            if (now_date - candidate).days > 180:
                candidate = date(now_date.year + 1, month, day)
            return candidate
        except (ValueError, TypeError):
            return None

    return None


class BaseScraper:
    """
    Base class for scraping match data from OddsPortal.
    """

    def __init__(
        self,
        playwright_manager: PlaywrightManager,
        browser_helper: BrowserHelper,
        market_extractor: OddsPortalMarketExtractor,
        preview_submarkets_only: bool = False,
    ):
        """
        Args:
            playwright_manager (PlaywrightManager): Handles Playwright lifecycle.
            browser_helper (BrowserHelper): Helper class for browser interactions.
            market_extractor (OddsPortalMarketExtractor): Handles market scraping.
            preview_submarkets_only (bool): If True, only scrape average odds from visible submarkets without loading
            individual bookmaker details.
        """
        self.logger = logging.getLogger(self.__class__.__name__)
        self.playwright_manager = playwright_manager
        self.browser_helper = browser_helper
        self.market_extractor = market_extractor
        self.preview_submarkets_only = preview_submarkets_only

    async def set_odds_format(self, page: Page, odds_format: OddsFormat = OddsFormat.DECIMAL_ODDS):
        """
        Sets the odds format on the page.

        Args:
            page (Page): The Playwright page instance.
            odds_format (OddsFormat): The desired odds format.
        """
        try:
            self.logger.info(f"Setting odds format: {odds_format.value}")
            button_selector = "div.group > button.gap-2"
            await page.wait_for_selector(button_selector, state="attached", timeout=ODDS_FORMAT_SELECTOR_TIMEOUT_MS)
            dropdown_button = await page.query_selector(button_selector)

            # Check if the desired format is already selected
            current_format = await dropdown_button.inner_text()
            self.logger.info(f"Current odds format detected: {current_format}")

            if current_format == odds_format.value:
                self.logger.info(f"Odds format is already set to '{odds_format.value}'. Skipping.")
                return

            await dropdown_button.click()
            await page.wait_for_timeout(ODDS_FORMAT_WAIT_MS)
            format_option_selector = "div.group > div.dropdown-content > ul > li > a"
            format_options = await page.query_selector_all(format_option_selector)

            for option in format_options:
                option_text = await option.inner_text()

                if odds_format.value.lower() in option_text.lower():
                    self.logger.info(f"Selecting odds format: {option_text}")
                    await option.click()
                    await page.wait_for_timeout(ODDS_FORMAT_WAIT_MS)
                    self.logger.info(f"Odds format changed to '{odds_format.value}'.")
                    return

            self.logger.warning(f"Desired odds format '{odds_format.value}' not found in dropdown options.")

        except TimeoutError:
            self.logger.error("Timeout while setting odds format. Dropdown may not have loaded.")

        except Exception as e:
            self.logger.error(f"Error while setting odds format: {e}", exc_info=True)

    async def extract_match_links(self, page: Page, date_filter: date | None = None) -> list[str]:
        """
        Extract and parse match links from the current page.

        Event rows on OddsPortal listing pages are grouped by date: the first
        row of a group carries a `[data-testid='date-header']` element, and
        subsequent rows in the same group inherit that date. When `date_filter`
        is provided, rows are iterated in document order, the "current" date
        header is tracked, and only rows whose group matches the filter are
        kept.

        Args:
            page (Page): A Playwright Page instance for this task.
            date_filter (Optional[date]): If provided, keep only match links
                whose surrounding date-header matches this date. Rows under a
                date-header that cannot be parsed are kept (fail-safe).

        Returns:
            List[str]: A list of unique match links found on the page.
        """
        try:
            html_content = await page.content()
            soup = BeautifulSoup(html_content, "lxml")
            event_rows = soup.find_all(class_=re.compile(OddsPortalSelectors.EVENT_ROW_CLASS_PATTERN))
            self.logger.info(f"Found {len(event_rows)} event rows.")

            tz_name = getattr(self.playwright_manager, "timezone_id", None) if date_filter else None

            seen: set[str] = set()
            match_links: list[str] = []
            current_row_date: date | None = None
            filtered_out_count = 0
            unparseable_header_count = 0

            for row in event_rows:
                if date_filter is not None:
                    header_el = row.find(attrs={"data-testid": "date-header"})
                    if header_el is not None:
                        header_text = header_el.get_text(" ", strip=True)
                        parsed = _parse_date_header(header_text, tz_name=tz_name)
                        if parsed is None:
                            unparseable_header_count += 1
                            self.logger.warning(
                                f"Could not parse date-header '{header_text}'; rows under it will not be filtered."
                            )
                        current_row_date = parsed

                    if current_row_date is not None and current_row_date != date_filter:
                        filtered_out_count += 1
                        continue

                for link in row.find_all("a", href=True):
                    href = link["href"]
                    if len(href.strip("/").split("/")) <= 3:
                        continue
                    full_url = f"{ODDSPORTAL_BASE_URL}{href}"
                    if full_url not in seen:
                        seen.add(full_url)
                        match_links.append(full_url)

            if date_filter is not None:
                self.logger.info(
                    f"Extracted {len(match_links)} unique match links after date filtering "
                    f"(filter={date_filter.isoformat()}, filtered out {filtered_out_count} rows, "
                    f"{unparseable_header_count} unparseable headers)."
                )
            else:
                self.logger.info(f"Extracted {len(match_links)} unique match links.")

            return match_links

        except Exception as e:
            self.logger.error(f"Error extracting match links: {e}", exc_info=True)
            return []

    async def extract_match_odds(
        self,
        sport: str,
        match_links: list[str],
        markets: list[str] | None = None,
        scrape_odds_history: bool = False,
        target_bookmaker: str | None = None,
        concurrent_scraping_task: int = 3,
        preview_submarkets_only: bool = False,
        bookies_filter: BookiesFilter = BookiesFilter.ALL,
        period: Enum | None = None,
        retry_config: RetryConfig | None = None,
        request_delay: float = DEFAULT_REQUEST_DELAY_S,
    ) -> ScrapeResult:
        """
        Extract odds for a list of match links concurrently.

        Args:
            sport (str): The sport to scrape odds for.
            match_links (List[str]): A list of match links to scrape odds for.
            markets (Optional[List[str]]: The list of markets to scrape.
            scrape_odds_history (bool): Whether to scrape and attach odds history.
            target_bookmaker (str): If set, only scrape odds for this bookmaker.
            concurrent_scraping_task (int): Controls how many pages are processed simultaneously.
            preview_submarkets_only (bool): If True, only scrape average odds from visible submarkets without loading
            individual bookmaker details.
            bookies_filter (BookiesFilter): The bookmaker filter to apply.
            period: The period to scrape odds for.
            retry_config: Configuration for per-match retry behavior.

        Returns:
            ScrapeResult: Contains successful results, failed URLs with error details, and statistics.
        """
        self.logger.info(f"Starting to scrape odds for {len(match_links)} match links...")

        result = ScrapeResult(stats=ScrapeStats(total_urls=len(match_links)))
        semaphore = asyncio.Semaphore(concurrent_scraping_task)

        if retry_config is None:
            retry_config = RetryConfig(
                max_attempts=MATCH_RETRY_MAX_ATTEMPTS,
                base_delay=MATCH_RETRY_BASE_DELAY,
                max_delay=MATCH_RETRY_MAX_DELAY,
            )

        async def scrape_single_match(page: Page, link: str) -> dict[str, Any] | None:
            """Inner function to scrape a single match (used for retry)."""
            return await self._scrape_match_data(
                page=page,
                sport=sport,
                match_link=link,
                markets=markets,
                scrape_odds_history=scrape_odds_history,
                target_bookmaker=target_bookmaker,
                preview_submarkets_only=preview_submarkets_only,
                bookies_filter=bookies_filter,
                period=period,
            )

        request_counter = {"count": 0}

        async def scrape_with_semaphore(link: str) -> tuple[str, dict[str, Any] | None, FailedUrl | None]:
            async with semaphore:
                # Apply rate limiting delay (skip for the first request)
                current_count = request_counter["count"]
                request_counter["count"] += 1
                if current_count > 0 and request_delay > 0:
                    jitter = request_delay * REQUEST_DELAY_JITTER_FACTOR * random.random()  # noqa: S311
                    total_delay = request_delay + jitter
                    self.logger.debug(f"Rate limiting: waiting {total_delay:.2f}s before request")
                    await asyncio.sleep(total_delay)

                tab = None

                try:
                    tab = await self.playwright_manager.context.new_page()

                    # Use retry with backoff for each match
                    retry_result = await retry_with_backoff(
                        scrape_single_match,
                        tab,
                        link,
                        config=retry_config,
                    )

                    if retry_result.success and retry_result.result is not None:
                        self.logger.info(f"Successfully scraped match link: {link} (attempts: {retry_result.attempts})")
                        return (link, retry_result.result, None)
                    else:
                        # Scraping failed after retries
                        error_type = retry_result.error_type or classify_error(retry_result.last_error)
                        failed_url = FailedUrl(
                            url=link,
                            error_type=error_type,
                            error_message=retry_result.last_error or "Unknown error",
                            attempts=retry_result.attempts,
                            is_retryable=is_retryable_error(retry_result.last_error or ""),
                        )
                        self.logger.warning(
                            f"Failed to scrape {link} after {retry_result.attempts} attempts: {retry_result.last_error}"
                        )
                        return (link, None, failed_url)

                except Exception as e:
                    # Unexpected error outside of retry mechanism
                    error_message = str(e)
                    failed_url = FailedUrl(
                        url=link,
                        error_type=classify_error(error_message),
                        error_message=error_message,
                        attempts=1,
                        is_retryable=is_retryable_error(error_message),
                    )
                    self.logger.error(f"Unexpected error scraping {link}: {e}")
                    return (link, None, failed_url)

                finally:
                    if tab:
                        await tab.close()

        # Execute all scraping tasks concurrently
        tasks = [scrape_with_semaphore(link) for link in match_links]
        results = await asyncio.gather(*tasks)

        # Process results
        for _link, data, failed_url in results:
            if data is not None:
                result.success.append(data)
                result.stats.successful += 1
            elif failed_url is not None:
                result.failed.append(failed_url)
                result.stats.failed += 1

        # Log summary
        self.logger.info(
            f"Scraping complete: {result.stats.successful}/{result.stats.total_urls} successful "
            f"({result.stats.success_rate:.1f}%)"
        )

        if result.failed:
            retryable_count = len(result.get_retryable_urls())
            self.logger.warning(
                f"Failed to scrape {result.stats.failed} URLs "
                f"({retryable_count} retryable, {result.stats.failed - retryable_count} permanent)"
            )
            # Log error breakdown
            error_breakdown = result.get_error_breakdown()
            for error_type, urls in error_breakdown.items():
                self.logger.debug(f"  {error_type}: {len(urls)} URLs")

        return result

    async def _scrape_match_data(
        self,
        page: Page,
        sport: str,
        match_link: str,
        markets: list[str] | None = None,
        scrape_odds_history: bool = False,
        target_bookmaker: str | None = None,
        preview_submarkets_only: bool = False,
        bookies_filter: BookiesFilter = BookiesFilter.ALL,
        period: Enum | None = None,
    ) -> dict[str, Any] | None:
        """
        Scrape data for a specific match based on the desired markets.

        Args:
            page (Page): A Playwright Page instance for this task.
            sport (str): The sport to scrape odds for.
            match_link (str): The link to the match page.
            markets (Optional[List[str]]): A list of markets to scrape (e.g., ['1x2', 'over_under_2_5']).
            scrape_odds_history (bool): Whether to scrape and attach odds history.
            target_bookmaker (str): If set, only scrape odds for this bookmaker.
            preview_submarkets_only (bool): If True, only scrape average odds from visible submarkets without loading
            individual bookmaker details.
            bookies_filter (BookiesFilter): The bookmaker filter to apply.
            period: The period enum to scrape odds for (FootballPeriod, TennisPeriod, or BasketballPeriod).

        Returns:
            Optional[Dict[str, Any]]: A dictionary containing scraped data, or None if scraping fails.
        """
        self.logger.info(f"Scraping match: {match_link}")

        try:
            # Navigate to the match page with extended timeout
            resp = await page.goto(match_link, timeout=NAVIGATION_TIMEOUT_MS, wait_until="domcontentloaded")
            resp_status = resp.status if resp else "no-response"
            body_len = len(await page.content())
            self.logger.info(f"match goto status={resp_status} body_bytes={body_len} url={page.url}")

            # Wait a bit for dynamic content to load
            await page.wait_for_timeout(DYNAMIC_CONTENT_WAIT_MS)

            # Apply bookmaker filter before extracting odds
            await self.browser_helper.ensure_bookies_filter_selected(page=page, desired_filter=bookies_filter)

            match_details = await self._extract_match_details_event_header(page, match_link)

            if not match_details:
                self.logger.warning(
                    f"No match details found for {match_link} - page may be unavailable or structure changed"
                )
                return None

            if markets:
                self.logger.info(f"Scraping markets: {markets}")
                try:
                    # Convert period enum to internal value for market extractor
                    # If period is None, get_internal_value will return None and market extractor will use default
                    period_internal = period.get_internal_value(period) if period else None
                    market_data = await self.market_extractor.scrape_markets(
                        page=page,
                        sport=sport,
                        markets=markets,
                        period=period_internal,
                        scrape_odds_history=scrape_odds_history,
                        target_bookmaker=target_bookmaker,
                        preview_submarkets_only=preview_submarkets_only,
                    )
                    if market_data:
                        match_details.update(market_data)
                    else:
                        self.logger.warning(f"No market data found for {match_link}")
                except Exception as market_error:
                    self.logger.error(f"Error scraping markets for {match_link}: {market_error}")
                    # Continue without market data rather than failing completely

            return match_details

        except Exception as e:
            self.logger.error(f"Error scraping match data from {match_link}: {e}")
            return None

    async def _extract_match_details_event_header(self, page: Page, match_link: str) -> dict[str, Any] | None:
        """
        Extract match details such as date, teams, and scores from the react event header.

        Args:
            page (Page): A Playwright Page instance for this task.
            match_link (str): The link to the match page.

        Returns:
            Optional[Dict[str, Any]]: A dictionary containing match details, or None if header is not found.
        """
        try:
            # Wait for the react event header to be loaded
            try:
                await page.wait_for_selector("#react-event-header", timeout=SELECTOR_TIMEOUT_MS)
            except Exception:
                # If we can't find the selector, try to get the content anyway
                self.logger.warning("React event header selector not found, attempting to parse existing content")

            html_content = await page.content()
            soup = BeautifulSoup(html_content, "html.parser")
            event_header_div = soup.find("div", id="react-event-header")

            if not event_header_div:
                self.logger.warning("React event header div not found in page content")
                return None

            # Check if the div has the 'data' attribute
            data_attribute = event_header_div.get("data")
            if not data_attribute:
                self.logger.warning("React event header div found but 'data' attribute is missing")
                return None

            try:
                json_data = json.loads(data_attribute)
            except (TypeError, json.JSONDecodeError) as e:
                self.logger.error(f"Failed to parse JSON data from react event header: {e}")
                return None

            event_body = json_data.get("eventBody", {})
            event_data = json_data.get("eventData", {})
            unix_timestamp = event_body.get("startDate")

            match_date = (
                datetime.fromtimestamp(unix_timestamp, tz=UTC).strftime("%Y-%m-%d %H:%M:%S %Z")
                if unix_timestamp
                else None
            )

            return {
                "scraped_date": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S %Z"),
                "match_date": match_date,
                "match_link": match_link,
                "home_team": event_data.get("home"),
                "away_team": event_data.get("away"),
                "league_name": event_data.get("tournamentName"),
                "home_score": event_body.get("homeResult"),
                "away_score": event_body.get("awayResult"),
                "partial_results": clean_html_text(event_body.get("partialresult")),
                "venue": event_body.get("venue").encode("ascii", "ignore").decode("ascii")
                if event_body.get("venue")
                else None,
                "venue_town": event_body.get("venueTown").encode("ascii", "ignore").decode("ascii")
                if event_body.get("venueTown")
                else None,
                "venue_country": event_body.get("venueCountry"),
            }

        except Exception as e:
            self.logger.error(f"Error extracting match details while parsing React event header: {e}")
            return None
