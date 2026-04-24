from unittest.mock import AsyncMock, MagicMock

import pytest

from oddsharvester.core.browser_helper import BrowserHelper
from oddsharvester.core.market_extraction.navigation_manager import NavigationManager
from oddsharvester.utils.constants import DEFAULT_MARKET_TIMEOUT_MS, MARKET_SWITCH_WAIT_TIME_MS, SCROLL_PAUSE_TIME_MS


class TestNavigationManager:
    """Unit tests for the NavigationManager class."""

    @pytest.fixture
    def browser_helper_mock(self):
        """Create a mock for BrowserHelper."""
        return MagicMock(spec=BrowserHelper)

    @pytest.fixture
    def navigation_manager(self, browser_helper_mock):
        """Create an instance of NavigationManager with a mocked BrowserHelper."""
        return NavigationManager(browser_helper_mock)

    @pytest.fixture
    def page_mock(self):
        """Create a mock for the Playwright page."""
        mock = AsyncMock()
        mock.wait_for_timeout = AsyncMock()
        return mock

    @pytest.mark.asyncio
    async def test_navigate_to_market_tab_url_suffix_path(self, navigation_manager, page_mock, browser_helper_mock):
        """Registered markets (e.g., 1X2) navigate via URL hash — no click-based fallback is used."""
        # Arrange
        page_mock.url = "https://www.oddsportal.com/football/h2h/arsenal/southampton/#hnqjYTeK"
        page_mock.goto = AsyncMock()
        page_mock.wait_for_selector = AsyncMock()

        # Act
        result = await navigation_manager.navigate_to_market_tab(page_mock, "1X2")

        # Assert
        assert result is True
        page_mock.goto.assert_called_once()
        # URL includes the 1X2 hash-suffix
        assert page_mock.goto.call_args.args[0].endswith(":1X2;2")
        # Fallback to click-based helper is never invoked for URL-suffix markets
        browser_helper_mock.navigate_to_market_tab.assert_not_called()

    @pytest.mark.asyncio
    async def test_navigate_to_market_tab_fallback_success(self, navigation_manager, page_mock, browser_helper_mock):
        """Unmapped markets fall back to the legacy click-based navigation."""
        # Arrange — "Double Chance" is a valid market but not in the URL-suffix mapping
        browser_helper_mock.navigate_to_market_tab = AsyncMock(return_value=True)

        # Act
        result = await navigation_manager.navigate_to_market_tab(page_mock, "Double Chance")

        # Assert
        assert result is True
        browser_helper_mock.navigate_to_market_tab.assert_called_once_with(
            page=page_mock, market_tab_name="Double Chance", timeout=DEFAULT_MARKET_TIMEOUT_MS
        )

    @pytest.mark.asyncio
    async def test_navigate_to_market_tab_failure(self, navigation_manager, page_mock, browser_helper_mock):
        """Failure on the click-based fallback propagates a False return."""
        # Arrange
        browser_helper_mock.navigate_to_market_tab = AsyncMock(return_value=False)

        # Act
        result = await navigation_manager.navigate_to_market_tab(page_mock, "NonExistentMarket")

        # Assert
        assert result is False

    @pytest.mark.asyncio
    async def test_wait_for_market_switch_success(self, navigation_manager, page_mock):
        """Test successful market switch wait."""
        # Arrange
        market_name = "Over/Under"
        mock_active_tab = AsyncMock()
        mock_active_tab.text_content = AsyncMock(return_value="Over/Under")
        page_mock.query_selector = AsyncMock(return_value=mock_active_tab)

        # Act
        result = await navigation_manager.wait_for_market_switch(page_mock, market_name)

        # Assert
        assert result is True
        page_mock.wait_for_timeout.assert_called_with(MARKET_SWITCH_WAIT_TIME_MS)

    @pytest.mark.asyncio
    async def test_wait_for_market_switch_wrong_market(self, navigation_manager, page_mock):
        """Test market switch wait with wrong market name."""
        # Arrange
        market_name = "Over/Under"
        mock_active_tab = AsyncMock()
        mock_active_tab.text_content = AsyncMock(return_value="1X2")
        page_mock.query_selector = AsyncMock(return_value=mock_active_tab)

        # Act
        result = await navigation_manager.wait_for_market_switch(page_mock, market_name)

        # Assert
        assert result is False

    @pytest.mark.asyncio
    async def test_wait_for_market_switch_no_active_tab(self, navigation_manager, page_mock):
        """Test market switch wait when no active tab is found."""
        # Arrange
        market_name = "Over/Under"
        page_mock.query_selector = AsyncMock(return_value=None)

        # Act
        result = await navigation_manager.wait_for_market_switch(page_mock, market_name)

        # Assert
        assert result is False

    @pytest.mark.asyncio
    async def test_wait_for_market_switch_exception_handling(self, navigation_manager, page_mock):
        """Test market switch wait with exception handling."""
        # Arrange
        market_name = "Over/Under"
        page_mock.query_selector = AsyncMock(side_effect=Exception("Test exception"))

        # Act
        result = await navigation_manager.wait_for_market_switch(page_mock, market_name)

        # Assert
        assert result is False

    @pytest.mark.asyncio
    async def test_select_specific_market_success(self, navigation_manager, page_mock, browser_helper_mock):
        """Test successful selection of a specific market."""
        # Arrange
        browser_helper_mock.scroll_until_visible_and_click_parent = AsyncMock(return_value=True)
        specific_market = "Over/Under 2.5"

        # Act
        result = await navigation_manager.select_specific_market(page_mock, specific_market)

        # Assert
        assert result is True
        browser_helper_mock.scroll_until_visible_and_click_parent.assert_called_once_with(
            page=page_mock,
            selector="div.flex.w-full.items-center.justify-start.pl-3.font-bold p",
            text=specific_market,
        )

    @pytest.mark.asyncio
    async def test_select_specific_market_failure(self, navigation_manager, page_mock, browser_helper_mock):
        """Test failed selection of a specific market."""
        # Arrange
        browser_helper_mock.scroll_until_visible_and_click_parent = AsyncMock(return_value=False)
        specific_market = "NonExistentMarket"

        # Act
        result = await navigation_manager.select_specific_market(page_mock, specific_market)

        # Assert
        assert result is False

    @pytest.mark.asyncio
    async def test_close_specific_market_success(self, navigation_manager, page_mock, browser_helper_mock):
        """Test successful closing of a specific market."""
        # Arrange
        browser_helper_mock.scroll_until_visible_and_click_parent = AsyncMock(return_value=True)
        specific_market = "Over/Under 2.5"

        # Act
        result = await navigation_manager.close_specific_market(page_mock, specific_market)

        # Assert
        assert result is True
        browser_helper_mock.scroll_until_visible_and_click_parent.assert_called_once_with(
            page=page_mock,
            selector="div.flex.w-full.items-center.justify-start.pl-3.font-bold p",
            text=specific_market,
        )

    @pytest.mark.asyncio
    async def test_close_specific_market_failure(self, navigation_manager, page_mock, browser_helper_mock):
        """Test failed closing of a specific market."""
        # Arrange
        browser_helper_mock.scroll_until_visible_and_click_parent = AsyncMock(return_value=False)
        specific_market = "NonExistentMarket"

        # Act
        result = await navigation_manager.close_specific_market(page_mock, specific_market)

        # Assert
        assert result is False

    @pytest.mark.asyncio
    async def test_wait_for_page_load(self, navigation_manager, page_mock):
        """Test waiting for page load."""
        # Act
        await navigation_manager.wait_for_page_load(page_mock)

        # Assert
        page_mock.wait_for_timeout.assert_called_once_with(SCROLL_PAUSE_TIME_MS)

    def test_constants(self):
        """Test that centralized constants have expected values."""
        assert DEFAULT_MARKET_TIMEOUT_MS == 20000
        assert SCROLL_PAUSE_TIME_MS == 2000
        assert MARKET_SWITCH_WAIT_TIME_MS == 3000
