import logging
import random

from playwright.async_api import async_playwright

from oddsharvester.utils.constants import PLAYWRIGHT_BROWSER_ARGS, PLAYWRIGHT_BROWSER_ARGS_DOCKER
from oddsharvester.utils.utils import is_running_in_docker

# Anti-detection script to hide automation signatures
STEALTH_SCRIPT = """
Object.defineProperty(navigator, "webdriver", {get: () => undefined});
window.chrome = {runtime: {}};
Object.defineProperty(navigator, "plugins", {get: () => [1, 2, 3, 4, 5]});
Object.defineProperty(navigator, "languages", {get: () => ["en-US", "en"]});
"""

# Default user agents that look like real browsers
DEFAULT_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
]


# Resource types blocked for bandwidth — measured savings of ~30% per match.
# Adding ``image`` here on top of font/media saves another ~20% but only when stylesheet
# is *also* allowed; aborting CSS in addition is the regression trigger that yields 0 rows.
_BLOCKED_RESOURCE_TYPES = frozenset({"font", "media", "image"})

# Hostnames whose requests are pure ads / analytics / consent banners.
_BLOCKED_HOST_KEYWORDS = (
    "googletagmanager",
    "google-analytics",
    "googlesyndication",
    "doubleclick",
    "googleadservices",
    "adservice",
    "facebook.net",
    "facebook.com",
    "hotjar",
    "scorecardresearch",
    "cookielaw.org",
    "onetrust.com",
    "surveygizmo",
    "widgixeu",
    "consensu.org",
    "criteo",
    "taboola",
    "outbrain",
    "amazon-adsystem",
    "doubleverify",
    "moatads",
    "yieldmo",
)


async def _route_block_unneeded(route, request):
    """Abort image/font/stylesheet/media requests and known ad/analytics hostnames."""
    try:
        if request.resource_type in _BLOCKED_RESOURCE_TYPES:
            await route.abort()
            return
        url_lower = request.url.lower()
        if any(host in url_lower for host in _BLOCKED_HOST_KEYWORDS):
            await route.abort()
            return
        await route.continue_()
    except Exception:  # pragma: no cover - playwright sometimes races on aborts
        try:
            await route.continue_()
        except Exception:
            pass


class PlaywrightManager:
    """
    Manages Playwright browser lifecycle and configuration.
    """

    def __init__(self):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.timezone_id: str | None = None

    async def initialize(
        self,
        headless: bool,
        user_agent: str | None = None,
        locale: str | None = None,
        timezone_id: str | None = None,
        proxy: dict[str, str] | None = None,
    ):
        """
        Initialize and start Playwright with a browser and page.

        Args:
            is_webdriver_headless (bool): Whether to start the browser in headless mode.
            proxy (Optional[Dict[str, str]]): Proxy configuration with keys 'server', 'username', and 'password'.
        """
        try:
            self.logger.info("Starting Playwright...")
            self.timezone_id = timezone_id
            self.playwright = await async_playwright().start()

            browser_args = PLAYWRIGHT_BROWSER_ARGS_DOCKER if is_running_in_docker() else PLAYWRIGHT_BROWSER_ARGS
            self.browser = await self.playwright.chromium.launch(headless=headless, args=browser_args, proxy=proxy)

            # Use provided user_agent or random default
            effective_user_agent = user_agent or random.choice(DEFAULT_USER_AGENTS)  # noqa: S311

            # OddsPortal's edge (Varnish/Cloudflare-like) started returning 503 to any request
            # that advertises a browser-class User-Agent coming from AWS IP ranges. It still
            # happily serves requests whose HTTP UA looks non-browser (e.g. "curl/7.88.1").
            # Playwright has a quirk: when both ``user_agent`` and ``extra_http_headers['User-Agent']``
            # are set, the context-level UA wins on the wire and the header is ignored. So we
            # drop ``user_agent`` and set ``extra_http_headers`` only — navigator.userAgent then
            # keeps Playwright's default HeadlessChrome string which the SPA still renders for.
            self.context = await self.browser.new_context(
                locale=locale,
                timezone_id=timezone_id,
                viewport={"width": random.randint(1366, 1920), "height": random.randint(768, 1080)},  # noqa: S311
                extra_http_headers={"User-Agent": "curl/7.88.1"},
            )

            # Add anti-detection script
            await self.context.add_init_script(STEALTH_SCRIPT)

            # Bandwidth-saving request interception: drop everything that isn't required for the
            # SPA to compute and render the bookmaker odds table. Cuts ~70% of egress when the
            # context is routed through a metered residential proxy.
            await self.context.route("**/*", _route_block_unneeded)

            self.page = await self.context.new_page()
            self.logger.info("Playwright initialized successfully.")

        except Exception as e:
            self.logger.error(f"Failed to initialize Playwright: {e!s}")
            raise

    async def cleanup(self):
        """Properly closes Playwright instances."""
        self.logger.info("Cleaning up Playwright resources...")
        if self.page:
            await self.page.close()
        if self.context:
            await self.context.close()
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
        self.logger.info("Playwright resources cleanup complete.")
