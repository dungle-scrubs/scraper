"""CLI entry point with stable JSON contract.

Subcommands:
  scrape  — scrape a URL and output JSON to stdout
  serve   — start the HTTP server

Exit codes:
  0  success (content scraped)
  1  scrape failed (all backends exhausted)
  2  invalid input (bad URL, malformed stdin JSON)
  3  timeout (global deadline exceeded)
  4  internal error (unexpected crash)

All logging goes to stderr.  stdout is exclusively for the JSON result.
"""

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import asdict

from scraper.safety import UrlRejected, validate_url
from scraper.scraper import ScrapeResult, scrape

# ── Exit codes ───────────────────────────────────────────────

EXIT_OK = 0
EXIT_SCRAPE_FAILED = 1
EXIT_INVALID_INPUT = 2
EXIT_TIMEOUT = 3
EXIT_INTERNAL = 4

DEFAULT_TIMEOUT_SECONDS = 120


# ── JSON helpers ─────────────────────────────────────────────


def _result_to_json(result: ScrapeResult, *, ok: bool) -> str:
    """Serialize a ScrapeResult to the stable JSON output format.

    @param result: Pipeline outcome.
    @param ok: Whether the scrape succeeded.
    @returns: JSON string ready for stdout.
    """
    payload = asdict(result)
    payload["ok"] = ok
    return json.dumps(payload)


def _error_json(error: str, *, url: str = "") -> str:
    """Build a minimal error JSON when the pipeline never ran.

    @param error: Human-readable error message.
    @param url: The URL that was attempted (empty if none).
    @returns: JSON string ready for stdout.
    """
    return json.dumps({"ok": False, "error": error, "url": url})


# ── stdin parsing ────────────────────────────────────────────


def _read_url_from_stdin() -> str:
    """Read a URL from a JSON object on stdin.

    Expected format: {"url": "https://..."}

    @returns: Validated URL string.
    @throws: SystemExit with EXIT_INVALID_INPUT on bad input.
    """
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(_error_json(f"Invalid JSON on stdin: {exc}"))
        raise SystemExit(EXIT_INVALID_INPUT) from exc

    url = data.get("url")
    if not url or not isinstance(url, str):
        print(_error_json('Missing or invalid "url" field in stdin JSON'))
        raise SystemExit(EXIT_INVALID_INPUT)

    return url


def _validate_url(url: str) -> None:
    """Guard: reject non-HTTP(S) schemes and internal/blocked SSRF targets.

    Delegates to the shared `safety.validate_url` guard so the CLI and server
    enforce identical policy.

    @param url: URL candidate to validate.
    @throws: SystemExit with EXIT_INVALID_INPUT on a bad or blocked URL.
    """
    try:
        validate_url(url)
    except UrlRejected as exc:
        print(_error_json(f"Invalid or blocked URL ({exc.reason}): {url}", url=url))
        raise SystemExit(EXIT_INVALID_INPUT) from exc


# ── Scrape runner ────────────────────────────────────────────


async def _run_scrape(url: str, timeout: int) -> tuple[ScrapeResult, int]:
    """Execute the scrape pipeline inside a global timeout.

    @param url: URL to scrape.
    @param timeout: Maximum wall-clock seconds for the entire pipeline.
    @returns: Tuple of (result, exit_code).
    """
    try:
        result = await asyncio.wait_for(scrape(url), timeout=timeout)
    except TimeoutError:
        result = ScrapeResult(url=url, error=f"Global timeout exceeded ({timeout}s)")
        return result, EXIT_TIMEOUT

    if result.error:
        return result, EXIT_SCRAPE_FAILED

    return result, EXIT_OK


# ── Subcommands ──────────────────────────────────────────────


def cmd_scrape(args: argparse.Namespace) -> None:
    """Scrape a URL and emit JSON to stdout.

    @param args: Parsed CLI arguments.
    """
    # Resolve URL from --stdin or positional arg.
    url: str = _read_url_from_stdin() if args.stdin else args.url
    if not url:
        print(_error_json("No URL provided"))
        raise SystemExit(EXIT_INVALID_INPUT)

    _validate_url(url)

    try:
        result, exit_code = asyncio.run(_run_scrape(url, args.timeout))
    except SystemExit:
        raise
    except Exception as exc:
        error_result = ScrapeResult(url=url, error=f"Internal error: {exc}")
        print(_result_to_json(error_result, ok=False))
        raise SystemExit(EXIT_INTERNAL) from exc

    print(_result_to_json(result, ok=exit_code == EXIT_OK))
    raise SystemExit(exit_code)


def cmd_serve(_args: argparse.Namespace) -> None:
    """Start the HTTP server (uvicorn).

    Uvicorn runs with its defaults (single worker, no `--limit-concurrency`).
    Concurrency bounding, the global per-request deadline, and the inbound body
    cap are enforced by the app itself (the shared semaphore, the timeout in
    `scrape_endpoint`, and `MaxBodySizeMiddleware`), independent of uvicorn's
    worker model. If horizontal scale is needed, run multiple processes behind a
    limiter rather than raising uvicorn's worker count (per-process Chromium
    pools share no state).

    @param _args: Parsed CLI arguments (unused, kept for dispatch signature).
    """
    import uvicorn

    from scraper.settings import settings

    uvicorn.run(
        "scraper.server:app",
        host=settings.host,
        port=settings.port,
        log_level="info",
    )


# ── Argument parser ──────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser with scrape/serve subcommands.

    @returns: Configured ArgumentParser.
    """
    parser = argparse.ArgumentParser(
        prog="scraper",
        description="Web scraper with anti-bot detection and Jina fallback.",
    )
    sub = parser.add_subparsers(dest="command")

    # ── scrape ───────────────────────────────────────────────
    scrape_p = sub.add_parser(
        "scrape",
        help="Scrape a URL and output JSON to stdout.",
    )
    scrape_p.add_argument(
        "url",
        nargs="?",
        default="",
        help="URL to scrape (omit when using --stdin).",
    )
    scrape_p.add_argument(
        "--stdin",
        action="store_true",
        help='Read {"url": "..."} from stdin instead of a positional argument.',
    )
    scrape_p.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"Global timeout in seconds (default: {DEFAULT_TIMEOUT_SECONDS}).",
    )

    # ── serve ────────────────────────────────────────────────
    sub.add_parser(
        "serve",
        help="Start the HTTP server.",
    )

    return parser


# ── Main ─────────────────────────────────────────────────────


def main() -> None:
    """Parse args and dispatch to the appropriate subcommand."""
    # Logs → stderr so stdout stays clean for JSON output.
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    parser = build_parser()
    args = parser.parse_args()

    if args.command == "scrape":
        cmd_scrape(args)
    elif args.command == "serve":
        cmd_serve(args)
    else:
        parser.print_help()
        raise SystemExit(0)
