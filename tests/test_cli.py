"""Tests for the CLI contract.

Validates JSON output format, exit codes, stdin handling, and timeout behavior.
All scraping calls are mocked — no network required.
"""

import asyncio
import json
from io import StringIO
from unittest.mock import AsyncMock, patch

import pytest

from scraper.cli import (
    DEFAULT_TIMEOUT_SECONDS,
    EXIT_INTERNAL,
    EXIT_INVALID_INPUT,
    EXIT_OK,
    EXIT_SCRAPE_FAILED,
    EXIT_TIMEOUT,
    _error_json,
    _read_url_from_stdin,
    _result_to_json,
    _validate_url,
    build_parser,
    cmd_scrape,
    cmd_serve,
    main,
)
from scraper.scraper import ScrapeResult

# ── JSON serialization ───────────────────────────────────────


class TestResultToJson:
    """Tests for _result_to_json output format."""

    def test_success_includes_ok_true(self) -> None:
        result = ScrapeResult(
            markdown="# Hello\n",
            source="crawl4ai",
            url="https://example.com",
            elapsed_ms=100.0,
            attempts=["crawl4ai attempt 1"],
        )
        parsed = json.loads(_result_to_json(result, ok=True))
        assert parsed["ok"] is True
        assert parsed["markdown"] == "# Hello\n"
        assert parsed["source"] == "crawl4ai"
        assert parsed["error"] is None

    def test_failure_includes_ok_false(self) -> None:
        result = ScrapeResult(
            url="https://example.com",
            error="All backends failed",
            elapsed_ms=5000.0,
            attempts=["crawl4ai attempt 1", "jina fallback"],
        )
        parsed = json.loads(_result_to_json(result, ok=False))
        assert parsed["ok"] is False
        assert parsed["error"] == "All backends failed"
        assert parsed["markdown"] == ""

    def test_all_fields_present(self) -> None:
        """Every ScrapeResult field plus 'ok' must appear in the JSON."""
        result = ScrapeResult(url="https://example.com")
        parsed = json.loads(_result_to_json(result, ok=False))
        expected_keys = {
            "ok",
            "markdown",
            "source",
            "url",
            "bot_detected",
            "error",
            "elapsed_ms",
            "attempts",
        }
        assert set(parsed.keys()) == expected_keys


class TestErrorJson:
    """Tests for _error_json minimal error output."""

    def test_basic_error(self) -> None:
        parsed = json.loads(_error_json("something broke"))
        assert parsed["ok"] is False
        assert parsed["error"] == "something broke"

    def test_includes_url_when_provided(self) -> None:
        parsed = json.loads(_error_json("bad", url="https://x.com"))
        assert parsed["url"] == "https://x.com"


# ── URL validation ───────────────────────────────────────────


class TestValidateUrl:
    """Tests for URL scheme validation."""

    def test_http_accepted(self) -> None:
        _validate_url("http://example.com")  # Should not raise.

    def test_https_accepted(self) -> None:
        _validate_url("https://example.com")  # Should not raise.

    def test_no_scheme_rejected(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            _validate_url("example.com")
        assert exc_info.value.code == EXIT_INVALID_INPUT

    def test_ftp_rejected(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            _validate_url("ftp://example.com")
        assert exc_info.value.code == EXIT_INVALID_INPUT

    def test_internal_ip_rejected(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            _validate_url("http://127.0.0.1:8020/admin")
        assert exc_info.value.code == EXIT_INVALID_INPUT

    def test_metadata_endpoint_rejected(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            _validate_url("http://169.254.169.254/latest/meta-data/")
        assert exc_info.value.code == EXIT_INVALID_INPUT


# ── stdin parsing ────────────────────────────────────────────


class TestReadUrlFromStdin:
    """Tests for JSON stdin parsing."""

    def test_valid_json(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.stdin", StringIO('{"url": "https://example.com"}'))
        assert _read_url_from_stdin() == "https://example.com"

    def test_malformed_json(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.stdin", StringIO("not json"))
        with pytest.raises(SystemExit) as exc_info:
            _read_url_from_stdin()
        assert exc_info.value.code == EXIT_INVALID_INPUT

    def test_missing_url_field(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.stdin", StringIO('{"uri": "https://example.com"}'))
        with pytest.raises(SystemExit) as exc_info:
            _read_url_from_stdin()
        assert exc_info.value.code == EXIT_INVALID_INPUT

    def test_empty_url_field(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.stdin", StringIO('{"url": ""}'))
        with pytest.raises(SystemExit) as exc_info:
            _read_url_from_stdin()
        assert exc_info.value.code == EXIT_INVALID_INPUT

    def test_non_string_url_field(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.stdin", StringIO('{"url": 123}'))
        with pytest.raises(SystemExit) as exc_info:
            _read_url_from_stdin()
        assert exc_info.value.code == EXIT_INVALID_INPUT


# ── Argument parser ──────────────────────────────────────────


class TestBuildParser:
    """Tests for CLI argument parsing."""

    def test_scrape_with_url(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["scrape", "https://example.com"])
        assert args.command == "scrape"
        assert args.url == "https://example.com"
        assert args.timeout == DEFAULT_TIMEOUT_SECONDS
        assert args.stdin is False

    def test_scrape_with_stdin_flag(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["scrape", "--stdin"])
        assert args.command == "scrape"
        assert args.stdin is True

    def test_scrape_custom_timeout(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["scrape", "--timeout", "30", "https://example.com"])
        assert args.timeout == 30

    def test_serve_subcommand(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["serve"])
        assert args.command == "serve"

    def test_no_subcommand(self) -> None:
        parser = build_parser()
        args = parser.parse_args([])
        assert args.command is None


# ── cmd_scrape integration ───────────────────────────────────


class TestCmdScrape:
    """Integration tests for the scrape subcommand dispatch."""

    @patch("scraper.cli.scrape", new_callable=AsyncMock)
    def test_success_exit_0(
        self, mock_scrape: AsyncMock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        mock_scrape.return_value = ScrapeResult(
            markdown="# Hello\n",
            source="crawl4ai",
            url="https://example.com",
            elapsed_ms=100.0,
            attempts=["crawl4ai attempt 1"],
        )
        parser = build_parser()
        args = parser.parse_args(["scrape", "https://example.com"])

        with pytest.raises(SystemExit) as exc_info:
            cmd_scrape(args)
        assert exc_info.value.code == EXIT_OK

        output = json.loads(capsys.readouterr().out)
        assert output["ok"] is True
        assert output["markdown"] == "# Hello\n"

    @patch("scraper.cli.scrape", new_callable=AsyncMock)
    def test_scrape_failure_exit_1(
        self, mock_scrape: AsyncMock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        mock_scrape.return_value = ScrapeResult(
            url="https://example.com",
            error="All backends failed",
            elapsed_ms=5000.0,
            attempts=["crawl4ai attempt 1", "jina fallback"],
        )
        parser = build_parser()
        args = parser.parse_args(["scrape", "https://example.com"])

        with pytest.raises(SystemExit) as exc_info:
            cmd_scrape(args)
        assert exc_info.value.code == EXIT_SCRAPE_FAILED

        output = json.loads(capsys.readouterr().out)
        assert output["ok"] is False
        assert output["error"] == "All backends failed"

    def test_no_url_exit_2(self, capsys: pytest.CaptureFixture[str]) -> None:
        parser = build_parser()
        args = parser.parse_args(["scrape"])

        with pytest.raises(SystemExit) as exc_info:
            cmd_scrape(args)
        assert exc_info.value.code == EXIT_INVALID_INPUT

        output = json.loads(capsys.readouterr().out)
        assert output["ok"] is False

    def test_bad_url_exit_2(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["scrape", "not-a-url"])

        with pytest.raises(SystemExit) as exc_info:
            cmd_scrape(args)
        assert exc_info.value.code == EXIT_INVALID_INPUT

    @patch("scraper.cli.scrape", new_callable=AsyncMock)
    def test_timeout_exit_3(
        self, mock_scrape: AsyncMock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        mock_scrape.side_effect = _hang_forever
        parser = build_parser()
        args = parser.parse_args(["scrape", "--timeout", "1", "https://example.com"])

        with pytest.raises(SystemExit) as exc_info:
            cmd_scrape(args)
        assert exc_info.value.code == EXIT_TIMEOUT

        output = json.loads(capsys.readouterr().out)
        assert output["ok"] is False
        assert "timeout" in output["error"].lower()

    @patch("scraper.cli.scrape", new_callable=AsyncMock)
    def test_internal_error_exit_4(
        self, mock_scrape: AsyncMock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        mock_scrape.side_effect = RuntimeError("kaboom")
        parser = build_parser()
        args = parser.parse_args(["scrape", "https://example.com"])

        with pytest.raises(SystemExit) as exc_info:
            cmd_scrape(args)
        assert exc_info.value.code == EXIT_INTERNAL

        output = json.loads(capsys.readouterr().out)
        assert output["ok"] is False
        assert "kaboom" in output["error"]

    @patch("scraper.cli.scrape", new_callable=AsyncMock)
    def test_stdin_mode(
        self,
        mock_scrape: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr("sys.stdin", StringIO('{"url": "https://example.com"}'))
        mock_scrape.return_value = ScrapeResult(
            markdown="# OK\n",
            source="jina",
            url="https://example.com",
            elapsed_ms=200.0,
            attempts=["jina fallback"],
        )
        parser = build_parser()
        args = parser.parse_args(["scrape", "--stdin"])

        with pytest.raises(SystemExit) as exc_info:
            cmd_scrape(args)
        assert exc_info.value.code == EXIT_OK

        output = json.loads(capsys.readouterr().out)
        assert output["ok"] is True
        assert output["source"] == "jina"


class TestCmdServe:
    """Tests for the server subcommand."""

    @patch("uvicorn.run")
    def test_invokes_uvicorn_with_settings(self, mock_run) -> None:
        args = build_parser().parse_args(["serve"])
        cmd_serve(args)
        mock_run.assert_called_once_with(
            "scraper.server:app",
            host="127.0.0.1",
            port=8020,
            log_level="info",
        )


class TestMain:
    """Tests for top-level CLI dispatch."""

    def test_dispatches_scrape(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["scrape", "https://example.com"])

        with (
            patch("scraper.cli.build_parser") as mock_build_parser,
            patch("scraper.cli.cmd_scrape") as mock_cmd_scrape,
        ):
            mock_build_parser.return_value.parse_args.return_value = args
            main()

        mock_cmd_scrape.assert_called_once_with(args)

    def test_dispatches_serve(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["serve"])

        with (
            patch("scraper.cli.build_parser") as mock_build_parser,
            patch("scraper.cli.cmd_serve") as mock_cmd_serve,
        ):
            mock_build_parser.return_value.parse_args.return_value = args
            main()

        mock_cmd_serve.assert_called_once_with(args)

    def test_no_subcommand_prints_help_and_exits_zero(self) -> None:
        parser = build_parser()
        no_command_args = parser.parse_args([])

        with (
            patch("scraper.cli.build_parser", return_value=parser),
            patch.object(parser, "parse_args", return_value=no_command_args),
            patch.object(parser, "print_help") as mock_print_help,
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == 0
        mock_print_help.assert_called_once_with()


# ── Helpers ──────────────────────────────────────────────────


async def _hang_forever(_url: str = "") -> ScrapeResult:
    """Coroutine that never completes — used to test timeout behavior.

    @param _url: Ignored — matches scrape() signature for use as AsyncMock side_effect.
    @returns: Never returns in practice.
    """
    await asyncio.sleep(3600)
    return ScrapeResult()  # pragma: no cover
