from datetime import UTC, datetime
import re

from oddsharvester.utils.constants import ODDSPORTAL_BASE_URL
from oddsharvester.utils.league_aliases import get_league_slug_for_season
from oddsharvester.utils.sport_league_constants import SPORTS_LEAGUES_URLS_MAPPING
from oddsharvester.utils.sport_market_constants import Sport

# Maps (main_market, specific_market) to the OddsPortal SPA hash-suffix used for
# direct URL navigation (bypasses flaky click-based tab selection). Keyed on the
# market names passed to extract_market_odds (e.g. main="1X2", specific=None or
# main="Over/Under", specific="Over/Under +2.5"). Limited scope on purpose —
# extend when another market proves click-nav unreliable.
_MARKET_URL_SUFFIX_MAPPING: dict[tuple[str, str | None], str] = {
    ("1X2", None): "1X2;2",
    # OddsPortal's SPA uses a lowercase ``bts`` slug for Both Teams to Score — capturing the URL
    # after clicking the tab shows ``#<hash>:bts;2`` even on pages where the upper-case ``BTTS``
    # label is rendered. Using the upper-case form here silently falls back to the 1X2 tab.
    ("Both Teams to Score", None): "bts;2",
    ("Over/Under", "Over/Under +2.5"): "Over/Under;2;2.5;0",
}


class URLBuilder:
    """
    A utility class for constructing URLs used in scraping data from OddsPortal.
    """

    @staticmethod
    def get_historic_matches_url(sport: str, league: str, season: str | None = None) -> str:
        """
        Constructs the URL for historical matches of a specific sport league and season.

        Args:
            sport (str): The sport for which the URL is required (e.g., "football", "tennis", "baseball").
            league (str): The league for which the URL is required (e.g., "premier-league", "mlb").
            season (Optional[str]): The season for which the URL is required. Accepts either:
                - a single year (e.g., "2024")
                - a range in 'YYYY-YYYY' format (e.g., "2023-2024")
                - None or empty string for the current season

        Returns:
            str: The constructed URL for the league and season.

        Raises:
            ValueError: If the season is provided but does not follow the expected format(s).
        """
        base_url = URLBuilder.get_league_url(sport, league).rstrip("/")

        # Resolve league alias for this season (handles sponsor name changes)
        alias_slug = get_league_slug_for_season(Sport(sport), league, season)
        if alias_slug:
            base_url = base_url.rsplit("/", 1)[0] + "/" + alias_slug

        # Treat missing season as current
        if not season:
            return f"{base_url}/results/"

        if isinstance(season, str) and season.lower() == "current":
            raise ValueError(f"Invalid season format: {season}. Expected format: 'YYYY' or 'YYYY-YYYY'")

        if re.match(r"^\d{4}$", season):
            return f"{base_url}-{season}/results/"

        if re.match(r"^\d{4}-\d{4}$", season):
            start_year, end_year = map(int, season.split("-"))
            if end_year != start_year + 1:
                raise ValueError(
                    f"Invalid season range: {season}. The second year must be exactly one year after the first."
                )

            # Special handling for baseball leagues
            if sport.lower() == "baseball":
                return f"{base_url}-{start_year}/results/"

            # OddsPortal serves the current season at the base URL (no year suffix)
            current_year = datetime.now(UTC).year
            if end_year == current_year:
                return f"{base_url}/results/"

            return f"{base_url}-{season}/results/"

        raise ValueError(f"Invalid season format: {season}. Expected format: 'YYYY' or 'YYYY-YYYY'")

    @staticmethod
    def get_upcoming_matches_url(sport: str, date: str, league: str | None = None) -> str:
        """
        Constructs the URL for upcoming matches for a specific sport and date.
        If a league is provided, includes the league in the URL.

        Args:
            sport (str): The sport for which the URL is required (e.g., "football", "tennis").
            date (str): The date for which the matches are required in 'YYYY-MM-DD' format (e.g., "2025-01-15").
            league (Optional[str]): The league for which matches are required (e.g., "premier-league").

        Returns:
            str: The constructed URL for upcoming matches.
        """
        if league:
            return URLBuilder.get_league_url(sport, league)
        return f"{ODDSPORTAL_BASE_URL}/matches/{sport}/{date}/"

    @staticmethod
    def get_league_url(sport: str, league: str) -> str:
        """
        Retrieves the URL associated with a specific league for a given sport.

        Args:
            sport (str): The sport name (e.g., "football", "tennis").
            league (str): The league name (e.g., "premier-league", "atp-tour").

        Returns:
            str: The URL associated with the league.

        Raises:
            ValueError: If the league is not found for the specified sport.
        """
        sport_enum = Sport(sport)

        if sport_enum not in SPORTS_LEAGUES_URLS_MAPPING:
            raise ValueError(f"Unsupported sport '{sport}'. Available: {', '.join(SPORTS_LEAGUES_URLS_MAPPING.keys())}")

        leagues = SPORTS_LEAGUES_URLS_MAPPING[sport_enum]

        if league not in leagues:
            raise ValueError(f"Invalid league '{league}' for sport '{sport}'. Available: {', '.join(leagues.keys())}")

        return leagues[league]

    @staticmethod
    def get_market_url_suffix(main_market: str, specific_market: str | None = None) -> str | None:
        """Return the OddsPortal URL-hash suffix for a (main_market, specific_market) pair.

        Returns None when the combination is not registered in ``_MARKET_URL_SUFFIX_MAPPING``,
        which callers can treat as "fall through to legacy click-based navigation".
        """
        return _MARKET_URL_SUFFIX_MAPPING.get((main_market, specific_market))

    @staticmethod
    def build_match_url_with_market(match_url: str, market_suffix: str) -> str:
        """Return ``match_url`` with the OddsPortal market hash-suffix applied.

        OddsPortal uses a SPA hash router of the form ``#<match_hash>[:<market>;<pos>[;<line>;<side>]]``.
        This helper preserves the match hash and replaces any prior market suffix.
        """
        if "#" not in match_url:
            return f"{match_url}#{market_suffix}"
        base, hash_part = match_url.split("#", 1)
        match_hash = hash_part.split(":", 1)[0]
        return f"{base}#{match_hash}:{market_suffix}"
