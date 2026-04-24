import logging

from playwright.async_api import Page

from oddsharvester.core.browser_helper import BrowserHelper
from oddsharvester.core.odds_portal_selectors import OddsPortalSelectors
from oddsharvester.core.url_builder import URLBuilder
from oddsharvester.utils.constants import (
    DEFAULT_MARKET_TIMEOUT_MS,
    DYNAMIC_CONTENT_WAIT_MS,
    MARKET_SWITCH_WAIT_TIME_MS,
    NAVIGATION_TIMEOUT_MS,
    SCROLL_PAUSE_TIME_MS,
    SELECTOR_TIMEOUT_MS,
)

# OddsPortal's SPA only boots cleanly with the ``1X2;2`` hash suffix — visiting a non-default
# suffix like ``bts;2`` directly leaves the bookmaker table empty. To render a non-default
# market we first boot on 1X2 and then trigger an in-page tab switch.
_BOOT_SUFFIX = "1X2;2"


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

        self.logger.info("Navigating via URL-suffix to %s (suffix=%s)", market_tab_name, url_suffix)
        try:
            # Always boot on the 1X2 suffix — OddsPortal's SPA does not initialise the bookmaker
            # table for non-default suffixes on a cold load. The reload after the goto guarantees
            # a clean SPA boot (a plain goto keeps the previous market's DOM when only the hash
            # changed).
            boot_url = URLBuilder.build_match_url_with_market(page.url, _BOOT_SUFFIX)
            await page.goto(boot_url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
            # Ads / trackers make ``networkidle`` unreliable here; ``domcontentloaded`` plus an
            # explicit wait for the bookmaker row selector is both faster and more deterministic.
            await page.reload(wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
            # A fresh match page can re-show the consent banner after reload; dismissing it before
            # waiting for rows avoids the banner masking click targets / slowing lazy-load.
            await self.browser_helper.dismiss_cookie_banner(page=page)
            await page.wait_for_selector(OddsPortalSelectors.BOOKMAKER_ROW_CSS, timeout=SELECTOR_TIMEOUT_MS)

            # For 1X2 the SPA has already rendered the desired tab; nothing else to do.
            if url_suffix == _BOOT_SUFFIX:
                return True

            # For non-default tabs, click the visible market label in the match page.
            # ``get_by_text`` typically returns two matches for the same text (one hidden
            # duplicate + one visible); we pick the visible one.
            tab_locator = page.get_by_text(market_tab_name, exact=True)
            tab_count = await tab_locator.count()
            clicked = False
            for i in range(tab_count):
                candidate = tab_locator.nth(i)
                if not await candidate.is_visible():
                    continue
                await candidate.scroll_into_view_if_needed(timeout=SELECTOR_TIMEOUT_MS)
                await candidate.click(timeout=SELECTOR_TIMEOUT_MS)
                clicked = True
                break
            if not clicked:
                self.logger.error("No visible tab with text %r on match page", market_tab_name)
                return False

            # Wait for the SPA to update the URL and re-render the bookmaker rows with the new
            # market's odds.
            await page.wait_for_url(lambda u, _suffix=url_suffix: _suffix in u, timeout=NAVIGATION_TIMEOUT_MS)
            await page.wait_for_timeout(DYNAMIC_CONTENT_WAIT_MS)
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
