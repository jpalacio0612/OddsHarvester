import logging

from playwright.async_api import Page

from oddsharvester.core.browser_helper import BrowserHelper
from oddsharvester.core.odds_portal_selectors import OddsPortalSelectors
from oddsharvester.core.url_builder import URLBuilder
from oddsharvester.utils.constants import (
    DEFAULT_MARKET_TIMEOUT_MS,
    MARKET_SWITCH_WAIT_TIME_MS,
    NAVIGATION_TIMEOUT_MS,
    SCROLL_PAUSE_TIME_MS,
    SELECTOR_TIMEOUT_MS,
)


class NavigationManager:
    """Handles browser navigation for market extraction."""

    def __init__(self, browser_helper: BrowserHelper):
        """Initialize NavigationManager."""
        self.logger = logging.getLogger(self.__class__.__name__)
        self.browser_helper = browser_helper

    async def navigate_to_market_tab(
        self,
        page: Page,
        market_tab_name: str,
        specific_market: str | None = None,
    ) -> bool:
        """Navigate to a market tab via OddsPortal URL-hash suffix routing.

        Replaces the legacy click-based tab navigation which was flaky on EU-region
        OddsPortal DOM. When the (main, specific) pair is registered in the URL-suffix
        mapping, we ``page.goto`` the match URL with the suffix appended so the SPA
        router renders the correct tab (and, for Over/Under, the specific line).
        Falls back to the legacy click-based helper when the pair is not mapped.
        """
        url_suffix = URLBuilder.get_market_url_suffix(market_tab_name, specific_market)

        if url_suffix is None:
            self.logger.debug(
                "Market not registered for URL-suffix nav (main=%r specific=%r); falling back to click-based nav",
                market_tab_name,
                specific_market,
            )
            return await self.browser_helper.navigate_to_market_tab(
                page=page, market_tab_name=market_tab_name, timeout=DEFAULT_MARKET_TIMEOUT_MS
            )

        full_url = URLBuilder.build_match_url_with_market(page.url, url_suffix)
        self.logger.info("Navigating via URL-suffix to %s: %s", market_tab_name, full_url)
        try:
            await page.goto(full_url, wait_until="networkidle", timeout=NAVIGATION_TIMEOUT_MS)
            await page.wait_for_selector(OddsPortalSelectors.BOOKMAKER_ROW_CSS, timeout=SELECTOR_TIMEOUT_MS)
            return True
        except Exception as e:
            self.logger.error("URL-suffix navigation failed for %r: %s", market_tab_name, e)
            return False

    async def wait_for_market_switch(self, page: Page, market_name: str, max_attempts: int = 3) -> bool:
        """
        Wait for the market switch to complete and verify the correct market is active.

        Args:
            page (Page): The Playwright page instance.
            market_name (str): The name of the market that should be active.
            max_attempts (int): Maximum number of verification attempts.

        Returns:
            bool: True if the market switch is confirmed, False otherwise.
        """
        self.logger.info(f"Waiting for market switch to complete for: {market_name}")

        for attempt in range(max_attempts):
            try:
                # Wait for the market switch animation to complete
                await page.wait_for_timeout(MARKET_SWITCH_WAIT_TIME_MS)

                # Check if the market tab is active
                active_tab = await page.query_selector("li.active, li[class*='active'], .active")
                if active_tab:
                    tab_text = await active_tab.text_content()
                    if tab_text and market_name.lower() in tab_text.lower():
                        self.logger.info(f"Market switch confirmed: {market_name} is active")
                        return True

            except Exception as e:
                self.logger.warning(f"Market switch verification attempt {attempt + 1} failed: {e}")

        self.logger.warning(f"Market switch verification failed after {max_attempts} attempts")
        return False

    async def select_specific_market(self, page: Page, specific_market: str) -> bool:
        """Select a specific submarket within the main market."""
        return await self.browser_helper.scroll_until_visible_and_click_parent(
            page=page,
            selector="div.flex.w-full.items-center.justify-start.pl-3.font-bold p",
            text=specific_market,
        )

    async def close_specific_market(self, page: Page, specific_market: str) -> bool:
        """Close a specific submarket after scraping."""
        self.logger.info(f"Closing sub-market: {specific_market}")
        return await self.browser_helper.scroll_until_visible_and_click_parent(
            page=page,
            selector="div.flex.w-full.items-center.justify-start.pl-3.font-bold p",
            text=specific_market,
        )

    async def wait_for_page_load(self, page: Page) -> None:
        """Wait for page content to load."""
        await page.wait_for_timeout(SCROLL_PAUSE_TIME_MS)
