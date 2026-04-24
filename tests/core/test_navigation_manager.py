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
        """1X2 is the boot tab: goto + reload is enough — no further click is performed."""
        # Arrange
        page_mock.url = "https://www.oddsportal.com/football/h2h/arsenal/southampton/#hnqjYTeK"
        page_mock.goto = AsyncMock()
        page_mock.reload = AsyncMock()
        page_mock.wait_for_selector = AsyncMock()

        # Act
        result = await navigation_manager.navigate_to_market_tab(page_mock, "1X2")

        # Assert
        assert result is True
        page_mock.goto.assert_called_once()
        assert page_mock.goto.call_args.args[0].endswith(":1X2;2")
        page_mock.reload.assert_called_once()
        # Fallback to click-based helper is never invoked for URL-suffix markets
        browser_helper_mock.navigate_to_market_tab.assert_not_called()

    @pytest.mark.asyncio
    async def test_navigate_to_market_tab_non_default_clicks_tab(
        self, navigation_manager, page_mock, browser_helper_mock
    ):
        """Non-default mapped markets (e.g. BTTS) boot on :1X2;2 then click the tab by text."""
        # Arrange
        page_mock.url = "https://www.oddsportal.com/football/h2h/arsenal/southampton/#hnqjYTeK"
        page_mock.goto = AsyncMock()
        page_mock.reload = AsyncMock()
        page_mock.wait_for_selector = AsyncMock()
        page_mock.wait_for_url = AsyncMock()
        page_mock.wait_for_timeout = AsyncMock()

        tab_mock = AsyncMock()
        tab_mock.count = AsyncMock(return_value=1)
        tab_mock.nth = MagicMock(return_value=tab_mock)
        tab_mock.is_visible = AsyncMock(return_value=True)
        tab_mock.scroll_into_view_if_needed = AsyncMock()
        tab_mock.click = AsyncMock()
        page_mock.get_by_text = MagicMock(return_value=tab_mock)

        # Act
        result = await navigation_manager.navigate_to_market_tab(page_mock, "Both Teams to Score")

        # Assert
        assert result is True
        # Boot goto lands on the :1X2;2 hash
        assert page_mock.goto.call_args.args[0].endswith(":1X2;2")
        # Tab was clicked by its visible text
        page_mock.get_by_text.assert_called_with("Both Teams to Score", exact=True)
        tab_mock.click.assert_called_once()
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
