"""scraper: web-scraping service with anti-bot detection and Jina fallback."""

from importlib.metadata import PackageNotFoundError, version

__all__ = ["__version__"]


def _resolve_version() -> str:
    """Return the installed package version, falling back to '0.0.0+unknown'.

    @returns: The package version string.
    """
    try:
        return version("dungle-scrubs-scraper")
    except PackageNotFoundError:
        # Running from source without an installed dist (e.g. raw checkout).
        return "0.0.0+unknown"


__version__ = _resolve_version()
